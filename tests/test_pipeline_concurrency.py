# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""同群并发串行化测试：基于管道语义桩（仅标准库，非完整管道验证）。

桩复刻 AstrBot 4.27.3 与本插件路径相关的管道语义，行号注释即源码位置：
- 调度器洋葱循环与 yield 后的 is_stopped() 检查：astrbot/core/pipeline/scheduler.py:44-74
- star_request 的 handler 串行执行、wrapper 耗尽后停止传播：astrbot/core/pipeline/process_stage/method/star_request.py:36-75
- call_handler 的异步生成器 / 协程分支：astrbot/core/pipeline/context_utils.py:47-75
- ProcessStage 的 LLM 兜底门槛（stop_event 写入空 STOP 结果后拦截）：astrbot/core/pipeline/process_stage/stage.py:52-66、astr_message_event.py:343-349
- ResultDecorate：OnDecoratingResult 钩子、回复装饰、内容安全仅 LLM 结果、钩子停止提前返回：astrbot/core/pipeline/result_decorate/stage.py:139-205
- RespondStage：空链跳过、发送、OnAfterMessageSent 钩子、吞发送异常：astrbot/core/pipeline/respond/stage.py:169-325
- stop_event / is_stopped / set_result 语义：astrbot/core/platform/astr_message_event.py:314-365
- EventBus 每条消息独立 create_task：astrbot/core/event_bus.py:52
- qqofficial 的 send() 覆盖实现不设置 _has_send_oper：qqofficial_message_event.py:111-113

未覆盖（非完整管道验证）：WakingCheck 过滤器、真实 ResultDecorate 的全部分支
（t2i / 分段回复 / 转发等）、流式输出、多插件注册顺序、真实平台适配器。
实机行为需按 README 人工验收步骤验证。

覆盖场景：
- 同群串行：后一条消息的 dispatch 晚于前一条回复的实际发出（互斥、状态不混入）；
- 跨群并行互不阻塞；
- 回复仍走完整装饰 / 钩子 / 发送管道（内容安全口径与现状一致：仅 LLM 结果）；
- 被认领消息 stop_event 拦截后续 handler 与 LLM 兜底；放行消息继续传播；
- 发送异常、任务取消、调度器遗弃生成器后，锁确定释放且同群后续请求可继续；
- 顺序边界：处理顺序跟随进入 handler 的顺序；前置阶段重排时处理顺序可以
  不等于消息到达顺序（互斥与状态不混入仍成立）。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

import test_dispatch

# 复用 test_dispatch 的 astrbot 桩：同一进程只安装一份桩、加载一份 main。
main = test_dispatch.main

# 场景曲库：第 7 首 Inferior 供「7 Inferior」猜歌；
# Testify 含字母 t，用于断言后到消息题板中 T 的显示。
SCENARIO_TITLES = [
    "Credits",
    "World Vanquisher",
    "千本桜",
    "Fracture Ray",
    "Grievous Lady",
    "Tempestissimo",
    "Inferior",
    "Testify",
]


class Result:
    """MessageEventResult 最小桩：chain + 停止标记；游戏回复非 LLM 结果。"""

    def __init__(self, text: str | None = None):
        self.chain: list[str] = [] if text is None else [text]
        self._stopped = False

    def stop_event(self) -> "Result":
        self._stopped = True
        return self

    def is_stopped(self) -> bool:
        return self._stopped

    def is_llm_result(self) -> bool:
        return False  # plain_result 产生的 GENERAL_RESULT

    def get_plain_text(self) -> str:
        return "".join(self.chain)


class PipelineEvent:
    """AstrMessageEvent 最小桩，含 stop_event / result / send 的忠实语义。"""

    def __init__(
        self,
        text: str,
        group_id: str,
        send_log: list[str],
        send_hook=None,
        pre_delay: float = 0.0,
    ):
        self.message_str = text
        self._chain = [main.Plain(text)] if text else []
        self._group_id = group_id
        self._send_log = send_log
        self._send_hook = send_hook  # async fn(event, text)，可注入延迟 / 异常
        self.pre_delay = pre_delay  # 进入 handler 前的可变延迟（模拟前置阶段重排）
        self.sent: list[str] = []
        # astr_message_event.py:58/343-365
        self._force_stopped = False
        self._result: Result | None = None
        # qqofficial 的 send() 覆盖实现不设置 _has_send_oper（见模块注释）。
        self.has_send_oper = False
        self.is_at_or_wake_command = True
        self.call_llm = False
        self.activated_handlers: list = []
        # 探针
        self.llm_probe_ran = False
        self.second_handler_ran = False
        self.content_safety_checked = False
        self.after_sent_texts: list[str] = []

    # ---- 插件与桩阶段用到的接口 ----

    def get_messages(self):
        return self._chain

    def get_group_id(self) -> str:
        return self._group_id

    def stop_event(self) -> None:  # astr_message_event.py:343-349
        self._force_stopped = True
        if self._result is None:
            self.set_result(Result().stop_event())
        else:
            self._result.stop_event()

    def is_stopped(self) -> bool:  # astr_message_event.py:359-365
        if self._force_stopped:
            return True
        if self._result is None:
            return False
        return self._result.is_stopped()

    def set_result(self, result: Result) -> None:  # astr_message_event.py:314-341
        self._result = result

    def get_result(self) -> Result | None:
        return self._result

    def clear_result(self) -> None:
        self._result = None

    def plain_result(self, text: str) -> Result:
        return Result(text)

    async def send(self, message: Result) -> None:
        """RespondStage 发送路径；send_hook 在记录前执行，记录即代表发送完成。"""
        text = message.get_plain_text()
        if self._send_hook is not None:
            await self._send_hook(self, text)
        self.sent.append(text)
        self._send_log.append(text)


class Pipeline:
    """管道语义桩：WakingCheck(协程) → Process(生成器) → ResultDecorate(生成器)
    → Respond(协程)。装饰与钩子可注入，用于验证回复仍走完整发送管道。"""

    def __init__(
        self,
        plugin,
        with_second_handler: bool = True,
        reply_prefix: str = "",
        decorating_hook=None,
        reply_content_safety: bool = False,
    ):
        self.plugin = plugin
        self._with_second_handler = with_second_handler
        self.reply_prefix = reply_prefix
        self.decorating_hook = decorating_hook
        self.reply_content_safety = reply_content_safety

    def execute(self, event: PipelineEvent):
        """模拟 EventBus：每条消息独立 create_task（event_bus.py:52）。"""
        return asyncio.create_task(self._execute(event))

    async def _execute(self, event: PipelineEvent) -> None:
        # 前置阶段语义桩：可变延迟挂起（如内容安全远程检查、限流等待），
        # 会改变消息进入 handler 的先后。
        if event.pre_delay:
            await asyncio.sleep(event.pre_delay)
        # WakingCheck 桩（协程，无 yield）：激活本插件 handler，
        # 可选附加「另一个插件」的 handler 探针。
        event.activated_handlers = [self.plugin.on_group_message]
        if self._with_second_handler:
            event.activated_handlers.append(self._second_handler)
        await self._process_stages(event)

    async def _second_handler(self, event: PipelineEvent) -> None:
        event.second_handler_ran = True

    async def _process_stages(self, event: PipelineEvent, from_stage: int = 0) -> None:
        """scheduler.py:44-74：生成器 Stage 的每个 yield 触发后续阶段递归，
        yield 后先检查 is_stopped()，已停止则跳过；协程 Stage 串行 await。"""
        stages = [self._process_stage, self._result_decorate, self._respond]
        for i in range(from_stage, len(stages)):
            proc = stages[i](event)
            if inspect.isasyncgen(proc):
                async for _ in proc:
                    if event.is_stopped():
                        break
                    await self._process_stages(event, i + 1)
                    if event.is_stopped():
                        break
            else:
                await proc
                if event.is_stopped():
                    break

    async def _process_stage(self, event: PipelineEvent):
        """ProcessStage.process + StarRequestSubStage.process 语义。"""
        # star_request.py:36-75
        for handler in event.activated_handlers:
            if event.is_stopped():
                break
            try:
                wrapper = self._call_handler(event, handler)
                async for _ret in wrapper:
                    yield
                if event.is_stopped():
                    break
                event.clear_result()
            except Exception:
                # star_request.py:54-74：记录错误并停止；错误回复路径不适用本插件。
                event.stop_event()
        # ProcessStage LLM 兜底门槛（process_stage/stage.py:52-66）：
        # stop_event() 在 result 为 None 时写入空 STOP 结果，
        # 使 get_result() 为真而 is_stopped() 为真，两个分支均不放行。
        if (
            not event.has_send_oper
            and event.is_at_or_wake_command
            and not event.call_llm
        ):
            if (event.get_result() and not event.is_stopped()) or not event.get_result():
                event.llm_probe_ran = True
                yield

    async def _call_handler(self, event: PipelineEvent, handler):
        """context_utils.py:47-75：异步生成器与协程两种 handler 分支。"""
        ready = handler(event)
        if inspect.isasyncgen(ready):
            has_yielded = False
            async for ret in ready:
                has_yielded = True
                if isinstance(ret, Result):
                    event.set_result(ret)
                    yield
                else:
                    yield ret
            if not has_yielded:
                yield
        else:
            ret = await ready
            if isinstance(ret, Result):
                event.set_result(ret)
                yield
            else:
                yield ret

    async def _result_decorate(self, event: PipelineEvent):
        """result_decorate/stage.py:126-205 语义桩：OnDecoratingResult 钩子、
        回复装饰（此处以前缀代表）、内容安全仅 LLM 结果、钩子停止提前返回。"""
        result = event.get_result()
        if result is None or not result.chain:
            return
        if self.reply_content_safety and result.is_llm_result():
            event.content_safety_checked = True
            yield  # :156 仅内容安全路径会挂起后续阶段
        if self.decorating_hook is not None:
            await self.decorating_hook(event)
            if event.is_stopped():
                return  # :184-189 钩子停止则提前结束本阶段
        if self.reply_prefix:
            result.chain[0] = self.reply_prefix + result.chain[0]

    async def _respond(self, event: PipelineEvent):
        """respond/stage.py:169-325 语义桩：空链跳过发送、吞发送异常、
        OnAfterMessageSent 钩子、清空结果。"""
        result = event.get_result()
        if result is None:
            return
        if len(result.chain) > 0:
            try:
                await event.send(result)
            except Exception:
                pass  # :313-320 发送失败记日志并吞掉
        event.after_sent_texts.append(result.get_plain_text())
        event.clear_result()


class NullLock:
    """无互斥锁桩：acquire 立即返回、locked 恒为 False，用于复现未加锁竞态。"""

    async def acquire(self):
        return True

    def locked(self) -> bool:
        return False


def make_plugin():
    plugin = main.MusicGuessPlugin(context=None, config={"exclusive_mode": False})
    plugin.song_pool = [f"Song {i}" for i in range(10)]
    plugin.song_load_error = None
    return plugin


def seed_game(plugin, group_id: str, titles=SCENARIO_TITLES) -> None:
    plugin.games[group_id] = main.GameState(
        songs=[main.RoundSong(title=title) for title in titles]
    )


def trace_dispatch(plugin, trace: list) -> None:
    """给 _dispatch_public 加进入 / 退出探针，用于断言串行顺序。"""
    original = plugin._dispatch_public

    def probed(group_id: str, text: str):
        trace.append((f"d-start:{text}", group_id))
        reply = original(group_id, text)
        trace.append((f"d-end:{text}", group_id))
        return reply

    plugin._dispatch_public = probed


class SameGroupOrderTests(unittest.TestCase):
    """同群互斥：后一条消息的 dispatch 晚于前一条回复的实际发出。"""

    def _scenario(self, *, lock_enabled: bool, trace: list, send_log: list):
        """「开t」先进入 handler、「7 Inferior」后进入；第一条回复的
        实际发送被闸门阻塞。"""
        plugin = make_plugin()
        seed_game(plugin, "G")
        if not lock_enabled:
            plugin._group_lock = lambda group_id: NullLock()
        trace_dispatch(plugin, trace)

        gate = asyncio.Event()

        async def gated_send(event: PipelineEvent, text: str):
            trace.append((f"send-enter:{event.message_str}", event.get_group_id()))
            await gate.wait()
            trace.append((f"send-done:{event.message_str}", event.get_group_id()))

        events = [
            PipelineEvent("开 t", "G", send_log, gated_send),
            PipelineEvent("7 Inferior", "G", send_log, gated_send),
        ]
        pipeline = Pipeline(plugin)
        return plugin, pipeline, events, gate

    def test_second_message_waits_for_first_reply_send(self):
        trace: list = []
        send_log: list = []

        async def run():
            plugin, pipeline, events, gate = self._scenario(
                lock_enabled=True, trace=trace, send_log=send_log
            )
            tasks = [pipeline.execute(e) for e in events]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # 第一条消息的回复已进入实际发送并被闸门阻塞，
            # 此时第二条必须尚未 dispatch。
            self.assertIn("send-enter:开 t", [t[0] for t in trace])
            self.assertNotIn("d-start:7 Inferior", [t[0] for t in trace])
            gate.set()
            await asyncio.gather(*tasks)
            return plugin

        plugin = asyncio.run(run())

        # 回复发出顺序与处理顺序一致。
        self.assertEqual(len(send_log), 2)
        self.assertIn("已开启 T", send_log[0])
        self.assertIn("答对了", send_log[1])

        # 第二条的 dispatch 严格晚于第一条回复发送完成（无状态混入窗口）。
        order = [t[0] for t in trace]
        self.assertLess(order.index("d-end:开 t"), order.index("send-enter:开 t"))
        self.assertLess(order.index("send-enter:开 t"), order.index("send-done:开 t"))
        self.assertLess(order.index("send-done:开 t"), order.index("d-start:7 Inferior"))
        self.assertLess(order.index("d-start:7 Inferior"), order.index("d-end:7 Inferior"))

        # 第二条回复的题板包含 T（第一条已完整完成）与第 7 首答对标记；
        # 第一条回复不含第 7 首答对标记。
        self.assertIn("T••t•••", send_log[1])  # Testify 行：t 已开启
        self.assertIn("✓ Inferior", send_log[1])
        self.assertNotIn("✓ Inferior", send_log[0])
        self.assertTrue(plugin.games["G"].songs[6].guessed)

    def test_without_lock_race_reproduces(self):
        """回归守卫：去掉互斥后，同样场景必然复现原缺陷——第二条在第一条
        回复发出前已 dispatch——证明上方测试确实能捕获该缺陷。"""
        trace: list = []
        send_log: list = []

        async def run():
            _plugin, pipeline, events, gate = self._scenario(
                lock_enabled=False, trace=trace, send_log=send_log
            )
            tasks = [pipeline.execute(e) for e in events]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*tasks)

        asyncio.run(run())

        order = [t[0] for t in trace]
        self.assertIn("d-end:7 Inferior", order)
        self.assertIn("send-done:开 t", order)
        # 未加锁时第二条的 dispatch 在第一条回复发出前执行。
        self.assertLess(order.index("d-end:7 Inferior"), order.index("send-done:开 t"))

    def test_mutual_exclusion_invariant_burst(self):
        """同群 5 条消息（每条发送让出一次事件循环）任意时刻至多一条
        处于 dispatch → 回复发出 区间。"""
        send_log: list = []
        trace: list = []
        plugin = make_plugin()
        seed_game(plugin, "G")
        trace_dispatch(plugin, trace)
        texts = ["开 c", "开 r", "开 f", "开 w", "开 i"]

        async def hook(event: PipelineEvent, _text: str):
            await asyncio.sleep(0)
            trace.append((f"send-done:{event.message_str}", event.get_group_id()))

        events = [PipelineEvent(text, "G", send_log, hook) for text in texts]

        async def run():
            pipeline = Pipeline(plugin)
            tasks = [pipeline.execute(e) for e in events]
            await asyncio.gather(*tasks)

        asyncio.run(run())

        self.assertEqual(len(send_log), 5)
        order = [t[0] for t in trace]
        for i, first in enumerate(texts):
            for second in texts[i + 1 :]:
                self.assertLess(
                    order.index(f"send-done:{first}"),
                    order.index(f"d-start:{second}"),
                    f"{second} 的 dispatch 必须晚于 {first} 的回复发出",
                )
                self.assertLess(
                    order.index(f"d-end:{first}"),
                    order.index(f"d-start:{second}"),
                    f"{second} 的 dispatch 必须晚于 {first} 的 dispatch 完成",
                )


class ReorderBoundaryTests(unittest.TestCase):
    """顺序边界：处理顺序跟随进入 handler 的顺序；前置阶段重排时，
    处理顺序可以不等于消息到达顺序，但互斥与状态不混入仍成立。"""

    def test_pre_stage_reorder_follows_handler_entry_order(self):
        send_log: list = []
        trace: list = []
        plugin = make_plugin()
        seed_game(plugin, "G")
        trace_dispatch(plugin, trace)

        # 到达顺序：先「开 r」后「开 c」；「开 r」的前置阶段有延迟，
        # 实际进入 handler 的顺序相反。
        trace.append(("arrive:开 r", "G"))
        trace.append(("arrive:开 c", "G"))
        slow = PipelineEvent("开 r", "G", send_log, pre_delay=0.05)
        fast = PipelineEvent("开 c", "G", send_log)

        async def run():
            pipeline = Pipeline(plugin)
            tasks = [pipeline.execute(slow), pipeline.execute(fast)]
            await asyncio.gather(*tasks)

        asyncio.run(run())

        order = [t[0] for t in trace]
        # 到达顺序：开 r 先于 开 c。
        self.assertLess(order.index("arrive:开 r"), order.index("arrive:开 c"))
        # 进入 handler / 处理 / 回复发出顺序：开 c 先于 开 r。
        self.assertLess(order.index("d-start:开 c"), order.index("d-start:开 r"))
        self.assertLess(send_log.index(next(s for s in send_log if "已开启 C" in s)),
                        send_log.index(next(s for s in send_log if "已开启 R" in s)))
        # 后处理的消息看到先处理消息的完整效果（互斥、不混入）：
        # 「开 r」的题板中 Credits 已显示 C 与 r；「开 c」的题板只显示 C。
        reply_c = next(s for s in send_log if "已开启 C" in s)
        reply_r = next(s for s in send_log if "已开启 R" in s)
        self.assertIn("C••••••", reply_c)  # Credits 仅开 c
        self.assertIn("Cr•••••", reply_r)  # Credits 已开 c 与 r


class CrossGroupTests(unittest.TestCase):
    def test_different_groups_do_not_block_each_other(self):
        send_log: list = []
        plugin = make_plugin()
        seed_game(plugin, "A")
        seed_game(plugin, "B")

        gate = asyncio.Event()

        async def gated_send(event, _text):
            await gate.wait()

        event_a = PipelineEvent("开 t", "A", send_log, gated_send)
        event_b = PipelineEvent("开 a", "B", send_log)

        async def run():
            pipeline = Pipeline(plugin)
            task_a = pipeline.execute(event_a)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # A 群消息的回复发送被阻塞时，B 群消息完整处理并回复。
            await asyncio.wait_for(pipeline.execute(event_b), timeout=2)
            self.assertEqual(len(send_log), 1)
            self.assertIn("已开启 A", send_log[0])
            self.assertIn("a", plugin.games["B"].opened_chars)
            self.assertNotIn("t", plugin.games["B"].opened_chars)
            gate.set()
            await task_a
            self.assertEqual(len(send_log), 2)

        asyncio.run(run())


class PipelineBehaviorTests(unittest.TestCase):
    """回复仍走完整装饰 / 钩子 / 发送管道；认领与放行的拦截语义不变。"""

    def _run_one(self, text: str, group_has_game: bool, group_id: str = "G", **kw):
        send_log: list = []
        plugin = make_plugin()
        if group_has_game:
            seed_game(plugin, group_id)
        event = PipelineEvent(text, group_id, send_log)

        async def run():
            await Pipeline(plugin, **kw).execute(event)

        asyncio.run(run())
        return plugin, event, send_log

    def test_reply_flows_through_decorate_and_hooks(self):
        decorate_hits: list[str] = []

        async def decorating_hook(event):
            result = event.get_result()
            if result is not None:
                decorate_hits.append(result.get_plain_text())

        plugin, event, send_log = self._run_one(
            "开 a",
            group_has_game=True,
            reply_prefix="[装饰] ",
            decorating_hook=decorating_hook,
            reply_content_safety=True,
        )

        # 全局回复装饰（前缀）作用于游戏回复，发送的是装饰后的文本。
        self.assertEqual(len(send_log), 1)
        self.assertTrue(send_log[0].startswith("[装饰] 已开启 A"))
        # OnDecoratingResult 钩子在发送前收到尚未加前缀的结果。
        self.assertEqual(len(decorate_hits), 1)
        self.assertTrue(decorate_hits[0].startswith("已开启 A"))
        # OnAfterMessageSent 钩子在发送后收到装饰后的结果。
        # （after_sent_texts 里的空串来自 stop 后 RespondStage 的平铺
        # 二跑——空 STOP 结果再次走到钩子，与 4.27.3 行为一致。）
        self.assertTrue(
            any(t.startswith("[装饰] 已开启 A") for t in event.after_sent_texts)
        )
        # 回复内容安全仅对 LLM 结果生效（result_decorate/stage.py:139-156），
        # 游戏回复与现状一致地不经过内容安全。
        self.assertFalse(event.content_safety_checked)

    def test_claimed_message_blocks_later_handlers_and_llm(self):
        plugin, event, send_log = self._run_one("开 a", group_has_game=True)

        self.assertTrue(event.is_stopped())
        self.assertFalse(event.second_handler_ran, "被认领消息不得进入后续 handler")
        self.assertFalse(event.llm_probe_ran, "被认领消息不得进入 LLM 兜底")
        self.assertEqual(len(send_log), 1, "恰好一次发送，无重复发送")
        self.assertIn("已开启 A", send_log[0])
        self.assertIn("a", plugin.games["G"].opened_chars)

    def test_passthrough_message_reaches_handlers_and_llm(self):
        _plugin, event, send_log = self._run_one("开 a", group_has_game=False)

        self.assertFalse(event.is_stopped())
        self.assertTrue(event.second_handler_ran, "放行消息应继续进入其他插件")
        self.assertTrue(event.llm_probe_ran, "放行消息应可进入 LLM 兜底")
        self.assertEqual(send_log, [])

    def test_help_command_reaches_handlers_and_llm(self):
        _plugin, event, send_log = self._run_one("/help", group_has_game=True)

        self.assertFalse(event.is_stopped())
        self.assertTrue(event.second_handler_ran)
        self.assertTrue(event.llm_probe_ran)
        self.assertEqual(send_log, [])


class RobustnessTests(unittest.TestCase):
    """发送异常 / 任务取消 / 调度器遗弃生成器后，锁确定释放，
    同群后续请求仍可继续。"""

    def test_send_exception_then_next_message_works(self):
        send_log: list = []
        plugin = make_plugin()
        seed_game(plugin, "G")

        async def raising_send(event, text):
            if text.startswith("已开启 T"):
                raise RuntimeError("QQ API down")
            await asyncio.sleep(0)

        event1 = PipelineEvent("开 t", "G", send_log, raising_send)
        event2 = PipelineEvent("开 a", "G", send_log)

        async def run():
            pipeline = Pipeline(plugin)
            await asyncio.wait_for(pipeline.execute(event1), timeout=2)
            # RespondStage 吞掉发送异常（respond/stage.py:313-320）：
            # handler 正常恢复、事件已 stop、状态已写入、无回复。
            self.assertTrue(event1.is_stopped())
            self.assertIn("t", plugin.games["G"].opened_chars)
            self.assertEqual(send_log, [])
            # 锁已释放，同群后续消息正常处理并回复。
            await asyncio.wait_for(pipeline.execute(event2), timeout=2)
            self.assertEqual(len(send_log), 1)
            self.assertIn("已开启 A", send_log[0])

        asyncio.run(run())

    def test_cancellation_releases_lock(self):
        send_log: list = []
        plugin = make_plugin()
        seed_game(plugin, "G")
        entered = asyncio.Event()

        async def gated_send(event, _text):
            entered.set()
            await asyncio.Event().wait()  # 永久阻塞：回复发送中途被取消

        event1 = PipelineEvent("开 t", "G", send_log, gated_send)
        event2 = PipelineEvent("开 a", "G", send_log)

        async def run():
            pipeline = Pipeline(plugin)
            task1 = pipeline.execute(event1)
            await asyncio.wait_for(entered.wait(), timeout=2)
            task1.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task1
            # 取消后 handler 生成器停留在挂起态，锁由管道任务完成回调
            # 按 token 确定释放：同群下一条消息可正常完成。
            await asyncio.wait_for(pipeline.execute(event2), timeout=2)
            self.assertEqual(len(send_log), 1)
            self.assertIn("已开启 A", send_log[0])

        asyncio.run(run())

    def test_abandoned_generator_releases_lock(self):
        """调度器在 handler 挂起期间因事件被停止而跳过生成器恢复
        （scheduler.py:66-70 的第二个 is_stopped 检查）：锁必须仍能由
        管道任务完成回调释放，同群后续消息不卡死。"""
        send_log: list = []
        plugin = make_plugin()
        seed_game(plugin, "G")
        stopped_once = asyncio.Event()

        async def decorating_hook(event):
            if not stopped_once.is_set():
                stopped_once.set()
                event.stop_event()  # 模拟 OnDecoratingResult 钩子终止事件

        async def hook(event, _text):
            await asyncio.sleep(0)

        event1 = PipelineEvent("开 t", "G", send_log, hook)
        event2 = PipelineEvent("开 a", "G", send_log, hook)

        async def run():
            pipeline = Pipeline(plugin, decorating_hook=decorating_hook)
            await asyncio.wait_for(pipeline.execute(event1), timeout=2)
            # 事件在装饰阶段被停止：回复仍被发出（RespondStage 在递归内
            # 运行），handler 生成器被遗弃、不再恢复。
            self.assertTrue(event1.is_stopped())
            self.assertEqual(len(send_log), 1)
            self.assertIn("已开启 T", send_log[0])
            # 遗弃路径下锁由任务完成回调释放：同群下一条消息可正常完成。
            await asyncio.wait_for(pipeline.execute(event2), timeout=2)
            self.assertEqual(len(send_log), 2)
            self.assertIn("已开启 A", send_log[1])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
