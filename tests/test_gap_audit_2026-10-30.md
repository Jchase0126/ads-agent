# 测试缺口审计报告（2026-10-30，Agent C）

- 范围：`tests/test_toolserver_busy.py`、`tests/test_cancel_chain.py`、`tests/test_turn_budget.py` 的替身与覆盖；`backend/agent.py` / `backend/server.py` / `backend/tools.py` / `addon/ads_agent/toolserver.py` 的取消链；`tests/run_tests.py` 注册。
- 行号基准：本日工作区当前版本（agent.py 2012 行 / server.py 1360 行 / tools.py 1297 行）。lead 接线继续进行会让行号漂移，引用时以符号名+行号双重定位。
- 纪律遵守：未修改任何被审计文件；全部行为验证走真实 HTTP / 真实模块调用；无固定 sleep 制造竞态；后台线程异常一律判失败；临时目录+随机端口；未打印 token。

---

## 0. 结论摘要（按严重度）

| # | 严重度 | 发现 | 证据 |
|---|--------|------|------|
| 1 | 高 | generator 替身使取消链测试"绿得巧合"：替身从未真实返回同步 dict | tests/test_cancel_chain.py:285-288 |
| 2 | 高 | 队列容量用例后台线程 TimeoutError 泄漏（PASS 带杂音、真实异常被吞） | tests/test_cancel_chain.py:153-174, :47, :174；tests/_harness.py:146-158 |
| 3 | 中 | pump 线程异常被静默吞掉（`except Exception: pass`） | tests/test_toolserver_busy.py:85-86 |
| 4 | 中 | 覆盖缺口×3：执行中取消最终收尾状态 / 请求超时后作业真实状态查询 / 取消-完成竞态（含 toolserver cid 残留锐边） | 见 §3 |
| 5 | 中 | 取消受理无即时 SSE 事件；执行中工具阻塞期间，客户端感知 `cancelled` 的最坏延迟 = 该工具 socket 超时（audit_rf 180s、run_python 600s 级） | backend/server.py:1044-1074, :1239-1247；backend/tools.py TIMEOUTS |
| 6 | 低 | 两个新测试未登记进 run_tests.py CASES | tests/run_tests.py:30-97 |
| ✅ | 已修复 | agent.py 普通工具派发不带 job_id → 取消链断点。**该缺口已由 lead 于 2026-10-30 修复**（register_job + job_id 透传 + propagate_cancel），本报告 §4 给出新证据，端到端 HTTP 测试已绿 | backend/agent.py:1485-1492 等 |

---

## 1. 后台线程 TimeoutError 被吞、用例仍 PASS（机制定位）

**现象**：`test_queue_capacity_rejects_with_busy` 运行结束后 stderr 出现未捕获 `TimeoutError` 回溯，但用例 PASS。findings.md 所记"取消容量用例有后台线程 TimeoutError"即此。

**证据链（四环缺一不泄漏）**：
1. `tests/test_cancel_chain.py:157` `_submit(n)` 的线程 target 只捕获 `toolserver.BusyError`；排队成功的 3 个线程在 `submit` 内 `done.wait(timeout+2.0)`（toolserver.py:590 附近，timeout=5 → 7s）。
2. `tests/test_cancel_chain.py:170` `t.join(timeout=3)`：3s 不够 7s，线程仍活着就继续往下走断言（join 超时不报错）。
3. `tests/test_cancel_chain.py:174` 断言通过后 `_reset_toolserver()`（:47 起）**排空 `_jobs` 队列但不 `done.set()`**——3 个等待线程在 ~7s 时抛 `TimeoutError`（非 BusyError，无人捕获）。
4. 未捕获异常走默认 `threading.excepthook`：只打 stderr，不影响退出码；而 `tests/_harness.py:146-158` `run()` **只统计主线程 test 函数的断言失败**，后台线程异常根本不进统计 → PASS 带杂音。

**影响**：CI 输出被假回溯污染，掩盖未来真实回归；且证明"后台线程异常必须判失败"这条纪律在现套件里没有机制保障。

**建议修法（实施：agent-b，文件所有者；lead 复核）**：
- 短平快：`_submit` 收集线程内所有异常类型（参照 test_toolserver_busy.py:143-154 的 `out` dict 模式），用例末尾断言 `out["error"]` 是预期类型或不存在；join 用 deadline 轮询 `not t.is_alive()` 而不是固定 3s。
- 机制性：tests/_harness.py（lead 所有）在 `run()` 前装 `threading.excepthook`，把非主线程未捕获异常记入全局失败列表，`run()` 末尾并入退出码。这一处改动可让全仓库测试受益。
- 触发器侧：`_reset_toolserver` 排空队列时应 `done.set()`（或只允许在确认无线程等待时调用），避免"制造"超时异常。

## 2. 替身审计：generator 误替同步 dict

**现象**：`tests/test_cancel_chain.py:285-288`：

```python
def call(self, cfg, name, args, job_id=""):
    executed.append(name)
    return {"ok": True}
    yield  # pragma: no cover   ← 这一行使整个函数成为 generator
```

函数体内存在 `yield` → `call()` 是 generator 函数：调用返回 generator 对象，**body 不执行**（`executed.append` 也不会跑）。

**为什么测试还绿**：`backend/agent.py:1495` 附近 `if "error" in result:` 对 dict 是键包含检查，对 generator 则触发**包含迭代**——惰性执行 body 到 `return`（迭代立即结束、无产出）→ `"error" in …` 为 False；随后 `isinstance(result, dict)` 为假 → 按 ok 处理。也就是说：返回值 `{"ok": True}` 从未被真实返回或校验，测试靠 agent 对非 dict 容错路径巧合通过，替身未模拟"真实 tools.call 同步返回 dict"的语义。

**同批检查**：
- tests/test_toolserver_busy.py：无 tools.call 替身问题（直接测 toolserver 本体）。
- tests/test_turn_budget.py：替身全部 `patch.object(tools_mod, "call", return_value={...})` 同步 dict，**无问题**。

**影响**：若 agent 未来收紧校验（如对非 dict result 直接判 error），该文件行为突变；且 `executed` 断言在 generator 场景下语义失真。

**建议修法（实施：agent-b）**：删除 `tests/test_cancel_chain.py:288` 的 `yield` 行即可（该行本就标了 pragma: no cover）；可加一行断言 `isinstance(result, dict)` 防回归。

## 3. tests/test_cancel_chain.py 覆盖缺口

现有覆盖（核对无误）：排队取消跳过(:73-94)、执行中取消只打标记(:114-128)、过期排队超时(:131-150)、队列容量(:153-174)、pump 预算(:177-194)、turn 取消传播×3(:201/:228/:274)、design 取消×2(:333/:360)、错误类(:415)。**缺**：

1. **执行中取消的最终收尾状态**：`request_cancel` 对 running 作业只置 `cancel_requested`（toolserver.py:363-396），但没有任何用例覆盖"打标记后的作业真实完成时，记录/结果如何收尾、调用方拿到什么"。结果归属（完成值 vs 取消标记谁赢）无行为断言。
2. **请求超时后作业仍执行的真实状态查询**：submit 抛 TimeoutError 后作业继续跑——`active_jobs()`（toolserver.py:420-433）此时应如实报告。test_toolserver_busy.py:274-290 只断言了 busy/timed_out 标记，未按 client_id/job_id 关联查询。**本报告交付 2（tests/test_submit_timeout_state.py）已补此缺口**（现状绿）。
3. **取消与完成竞态**：作业真实完成后 cancel 才到达 → toolserver.py:389-391 走"未知 client_id"分支，把 cid 记入 `_cancelled_ids` 并报 cancelled。锐边：**同 cid 的下一个排队作业会在 pump :631-637 被误判为已取消而跳过**（cid 残留直到被消费）。无任何用例覆盖；建议 agent-b 补"cancel-after-complete + 同 cid 再提交"的竞态用例，把该语义显式化（或修掉残留）。

另记（非缺口、稳定性建议）：该文件固定 sleep 点 :65/:86/:144/:168 在慢机上易抖，建议改事件+deadline 轮询（参照新交付的两份测试）。

## 4. agent.py `_run_tool_call` 与 `Turn.cancel` 的 job_id 断链 —— ✅ 已修复

**原缺口**（契约 v1 要求派发前创建 job_id 并登记 turn）：`tools_mod.call(self.cfg, name, args)` 不带 job_id → tools.call 内部自生成（tools.py:1186 附近，空则 uuid4）→ turn 不持有作业身份 → `/chat/cancel` 无从转发，链路在"轮次→工具作业"处断开。

**lead 已于 2026-10-30 修复**（当前工作区证据）：
- 派发点 backend/agent.py:1485-1492：`job_id = uuid.uuid4().hex` → `register_job(job_id, name, kind="ads")`（:941-946）→ `tools_mod.call(self.cfg, name, args, job_id=job_id)`。
- 转发链 backend/agent.py:965-1004 `propagate_cancel`：current_jobs（:960-963）→ ads 作业走 `tools_mod.cancel_ads_jobs(self.cfg, ads_ids)`（backend/tools.py:1277，POST toolserver /cancel）→ compute 作业走 compute_manager；回执是"已请求"而非"已退出"。
- 入口 backend/server.py:1044-1074 `/chat/cancel`：`turn.cancel()`（agent.py:928-935）+ `propagate_cancel()`，异常尽力而为不回 500（:1062-1067）。
- toolserver 侧 client_id 承接：toolserver.py:837 `client_id = str(body.get("job_id") or "")` → submit 按 cid 关联取消。
- **端到端行为证据**：tests/test_chat_cancel_http.py 全绿——假 ADS 在 `/execute` 收到的 job_id 与随后 `/cancel` 收到的 job_ids 完全一致（轮次→工具→作业贯穿的直接证据），SSE 流有界时间内结束、推送 cancelled、无 done、模型仅调用一次。

残留语义边界（如实记录，非缺陷）：`Turn.cancel` 本体（agent.py:928-935）仍只 set 事件；执行中作业的收尾依赖"下一个安全边界"（步前检查 agent.py:1164-1170、模型响应丢弃 :1218-1223、finalize :1306-1308、compute 兜底 :1821-1824）与 toolserver 的 cancel_requested 标记——与"绝不跨线程注入异常"的设计一致。

## 5. /chat/cancel 与 SSE：客户端等多久、有无取消事件

**现状（证据）**：
- `/chat/cancel`（server.py:1044-1074）的应答走 **POST 通道本身**：`{ok, known, jobs(回执), message}`。注：当前版本回执在 `"jobs"` 字段（propagate_cancel 返回 `{"ads":{cancelled,running,unknown}, "compute":…}`）；lead 计划的顶层 `cancelled/running/unknown` 数组在本版本尚未出现——新测试两种形态都接受，扁平化后主断言路径即命中。
- SSE 循环（server.py:1239-1247）只转发 turn worker 的 events：`events.get(timeout=15)` 空闲发 `b": ping\n\n"` keepalive。**没有**"取消已受理"的即时 SSE 事件。
- `cancelled` 事件由 turn 自己的取消检查点发出：步前 agent.py:1164-1170、模型响应后 :1218-1223、finalize :1306-1308。

**停止后客户端要等多久**：不是 1800s——服务端对 SSE 连接无整体 socket 超时，且 15s ping 保证代理不掐连接。真实上限是**执行中工具的 socket 超时**：tools.call 阻塞在 HTTP 等待上（socket timeout = 工具超时，tools.py TIMEOUTS：audit_rf 180s、run_python 600s 等，另加 30s margin），超时返回后 turn 才走到下一个取消检查点。取消发生在"模型流式等待"期间则响应返回即丢弃（:1218-1223），延迟小。**最坏情形 = 一个长工具的完整 socket 超时**（例：run_python ~630s），期间面板只有 ping。
- 已验证的好消息：取消会经 propagate_cancel 对排队作业直接跳过、对执行中作业打标记（toolserver /cancel），所以"下一个安全边界"通常远早于 socket 超时——前提是 handler 协作（如 JobContext 收到 cancel_requested 后收尾）。真实 ADS handler 对该标记的响应速度是 B 侧/实机验收项。

**建议修法（实施：lead，server.py 所有）**：可选增强——`/chat/cancel` 受理成功后向该 turn 的 events 队列注入一条 `{"type": "cancel_requested", "message": …}`，SSE 循环会立即取出推送，面板即时变灰而不必等安全边界。实现要点：`_register_turn`（server.py:112-118）已持有 turn_id→Turn 映射，需同时保留 events 队列引用（或给 Turn 挂 emit 回调）；注入与 worker 消费同队无竞态。新测试已把 cancelled 事件存在性断言就位，增强后无需改测试。

## 6. run_tests.py CASES 注册缺口

`tests/run_tests.py:30-97` 未登记两个新文件（均不需要 PySide6，建议层级 "http"，24 0s 上限内实测 ~10s 与 ~15s）：
- `tests/test_chat_cancel_http.py`（真实 HTTP 取消链）
- `tests/test_submit_timeout_state.py`（超时后作业状态现状）
**实施：lead**。

---

## 7. 已运行测试与结果（仅本人产出，未跑全量）

| 测试 | 结果 | 说明 |
|------|------|------|
| tests/test_submit_timeout_state.py | **PASS（1/1，绿）** | 记录现状：TimeoutError 后 handler 仍在跑；`active_jobs()` 如实报告 timed_out 标记；`/health` busy=True + timed_out_count≥1（标记与真实执行并存）；真实完成后记录摘除（当前语义：摘除=收尾，无独立 succeeded 终态记录，结果留在已丢弃 box）；pump/submit 后台线程零未捕异常 |
| tests/test_chat_cancel_http.py | **PASS（2/2，绿）** | 核心：假 ADS `/cancel` 收到 job_ids == `/execute` job_id（贯穿证据）；`/chat/cancel` 回执含该 job_id（当前版本经 `jobs` 字段命中）；SSE 有界结束（<30s 预算，实测即时）；cancelled 事件有、done 无；模型恰一次调用。附录用例：未知 turn_id → ok=False/known=False（server.py:1052-1056 如实语义） |

**终态历史依赖（登记）**：tests/test_submit_timeout_state.py 目前用现有 `active_jobs()/timed_out` 断言；agent-b 就绪的 `job_snapshot` 终态历史接入后，可在该测试第 ⑤ 步增补"作业 succeeded 终态记录"断言（接口名 `job_snapshot`），当前不阻塞。

---

## 8. 需要 lead 实施的公共文件修改清单

1. **tests/run_tests.py**（必做）：CASES 登记上述两个新测试，层级 `"http"`，needs_pySide6=False。
2. **tests/_harness.py**（建议）：安装 `threading.excepthook` 收集后台线程未捕获异常并入退出码（§1 机制性修法）；一次性消除"PASS 带假回溯"类问题。
3. **backend/server.py**（可选增强）：/chat/cancel 受理后向 turn 的 events 注入即时 `cancel_requested` SSE 事件（§5 要点）；如按计划把回执扁平化为顶层 `cancelled/running/unknown`，新测试无需改动即兼容。
4. **转交 agent-b 的修改建议**（B 所有文件，详见 §1/§2/§3）：删 test_cancel_chain.py:288 `yield`；容量用例改 deadline 轮询 + 后台异常断言 + `_reset_toolserver` 排空时 `done.set()`；`_Pump` 去掉 `except Exception: pass`（test_toolserver_busy.py:85-86）；补"cancel-after-complete 同 cid 再提交"竞态用例。
5. **提请注意**：本审计期间 lead 的接线还在推进，server.py 响应字段（顶层数组）与 agent-b 的 job_snapshot 落地后，请同步本报告 §5/§7 的行号与断言描述。

## 9. 限制与未完成项

- 未跑全量套件（lead 统一执行）；本人产出三项均已落盘并运行。
- 审计行号随 lead 接线漂移（已用符号名+行号双定位）；工具服务器内部取消语义（queued 跳过/running 标记）属 agent-b 的 test_cancel_chain 领域，本报告只登记缺口与锐边，不代写其测试。
- HTTP 测试的假 ADS 对执行中作业回 `cancelled`（真实 toolserver 对 running 作业回 `running`+cancel_requested 标记）：测试验证的是**后端转发与 job_id 贯穿**，不代替 toolserver 内部语义验证。
- "最坏取消感知延迟 = 长工具 socket 超时"（§5）未用真实 600s 工具实测（用假服务即时释放验证有界结束）；实机 ADS 对 cancel_requested 的响应速度是实机验收项，与 task_plan.md 的未验收清单并列。
- 取消与完成竞态的 cid 残留锐边（§3.3）只做了静态证据定位，未写成行为用例（属 test_cancel_chain 领域，避免越界修改）。
