# MetaGPT Pro 实验诊断：与 EvoMAS 历史成功轨迹对照

核查时间：2026-09-22 UTC。范围：最新 `workspace/pro19_api_paced_anomaly_rerun`、此前 `pro18_api_paced_smoke`、pro15–pro17 选样/调度记录，以及 EvoMAS 同题历史生成日志、补丁和评估结果。此次只读取证，不改运行代码、不调用模型、不重跑评估。逐题指标与输入 SHA-256 见 [诊断附件](SWE_pro_diagnosis_20260922.json)。

## 结论

目前不能把 MetaGPT 的表现概括为“mini 或多智能体能力不够”。已经确认三类独立问题：

1. **调度器把曾经发生的普通限流当成致命错误。** pro19 四个域都因此停队列，4 份非空补丁未评估，其中 2 份还获得 Reviewer approved。
2. **团队把剩余时间留空，而没有用于验证与返工。** 25 分钟预算的首轮编码在第 900 秒截止，审查 inconclusive 会立即终止整题；当前失败题仍剩约 8.17 分钟时即结束。
3. **历史“EvoMAS 成功”的对照池不干净。** 抽查到明确读取并应用目标修复提交的行为，断网也不能防止镜像内的 Git 历史泄漏。另有空补丁与 FULL 同时存在的记录，须追溯结果和补丁对应关系。

对于已正常完成评估的 Ansible 失败题，另有明确的实现/契约偏差，不能用前述调度问题解释掉。历史成功补丁可以帮助定位语义差异，但受污染的生成轨迹不能证明框架能力优于当前 MetaGPT。

## 1. 当前实际进度与运行条件

pro15 从 EvoMAS 各历史正式结果中选取“至少一次 FULL”的 111 个唯一实例，排除此前排入 pro11–pro14 的 3 题后计划 108 题；不是随机样本，也不是单一历史批次的干净成功子集。pro16 继续剩余 96 题；pro17 记录其中 59 题有明确累计额度错误，pro18 先复跑 1 题，pro19 接续另外 58 题。

截至核查，pro19 的四个 controller PID 均已不存在，各域 summary 的 stopped=true：

| 域 | 计划 | 已评估 | 成功 | 未解决 | blocked_api（已产补丁） | 未启动 |
|---|---:|---:|---:|---:|---:|---:|
| Ansible | 9 | 1 | 0 | 1 | 1 | 7 |
| Flipt | 20 | 0 | 0 | 0 | 1 | 19 |
| OpenLibrary | 18 | 1 | 1 | 0 | 1 | 16 |
| Webclients | 11 | 1 | 1 | 0 | 1 | 9 |
| 合计 | 58 | 3 | 2 | 1 | 4 | 51 |

不能把 2/58 作为完成实验解决率，也不能把 2/3 外推为全量表现。pro18 的单题是另一个失败样本，不属于 pro19 的 58 题分母。

实际架构是 **MetaGPT Leader + mini-swe-agent Engineer + MetaGPT Reviewer**，不是本地原生 Engineer；三角色的单次输出上限均为 16,384，整题预算 25 分钟，Leader 180 秒、Reviewer 180 秒，first_edit_fraction=0.6。当前源码与四个 pro19 manifest 的源码 SHA-256 均一致。生成轨迹记录 network=none 与 base-only 历史隔离。

## 2. 首要确定性缺陷：普通限流触发停批和漏评

[run_swe_full_experiment.py:51](../tests/metagpt/roles/di/run_swe_full_experiment.py#L51) 的 `fatal_api_error()` 同时匹配硬额度/认证错误和 `RateLimitError`、`rate limit exceeded`。

生成进程结束后，[179–181 行](../tests/metagpt/roles/di/run_swe_full_experiment.py#L179) 扫描整份 generation/worker 日志，只要历史上出现一次匹配就 `stop.set()`、标记 blocked_api 并 `break`，这个判断发生在读取已生成预测与进入 eval 之前。它不看错误后是否恢复，也不看是否已有有效补丁。

| pro19 实例短 ID | API 事件 | 最后一次限流后 mini 返回响应数 | 最终补丁字符数 | 最终审查 | 当前 eval |
|---|---|---:|---:|---|---|
| Ansible `c1f2df47` | 4 次 RateLimitError | 17 | 15,814 | approved | 未运行 |
| Flipt `3ef34d1` | 1 次 RateLimitError | 15 | 20,637 | inconclusive | 未运行 |
| OpenLibrary `427f1f4e` | 2 次 RateLimitError＋1 次 BadGatewayError | 13 | 3,859 | approved | 未运行 |
| Webclients `2f2f6c31` | 3 次 RateLimitError | 35 | 11,782 | inconclusive | 未运行 |

四题均有 `case_finished`，生成进程 returncode=0，worker 日志未发现预算耗尽/insufficient_quota/TokenStatusExhausted 标记。不能因此保证补丁正确，但可以确认它们不应仅因曾限流就丢失评估机会。OpenLibrary 最后一个模型错误是 BadGateway，需与先前 RateLimit 分开看。

pro16 的 59 题确有历史硬额度异常，与 pro19 的普通限流停批是不同问题；不能一律解释为“现在又没余额”。本次未请求 API，未验证当前账户余额。

本地 pacer 默认每 4.2 秒分配一个请求位置，只在共用 `/tmp/metagpt_swe_model_request_pacer` 的进程之间协调。它无法自动约束其他项目或其他主机对同一服务额度的使用，也不等于供应商的全账户限流保证。

**修复方向**：硬额度/认证失败才触发终止式暂停；瞬时限流采用有上限、尊重 deadline 的重试。分开记录 `had_transient_api_error`、`terminal_api_error` 和 `prediction_ready`；已有完整候选补丁应独立排入 eval，不受后续模型服务状态阻塞。4 份现存补丁优先仅补 eval，无需重新生成。

## 3. 团队预算和审查的提前退出

[swe_protocol.py:201](../metagpt/roles/di/swe_protocol.py#L201) 将首轮编辑截止设为整题 60% 的绝对截止点：25 分钟任务第 900 秒停止首轮编码，Leader 已耗时间也在这一窗口内。返工截止为总 deadline 减去 180 秒审查保留时间。

[reviewer.py:193](../metagpt/roles/di/reviewer.py#L193) 对 inconclusive 直接 `state.finish()`。因此补丁缺少成功验证时，可以在还剩较多时间的情况下结束整题；并非“25 分钟都交给 mini 使用”。此外 [109 行](../metagpt/roles/di/reviewer.py#L109) 在一次探索回合完成至少一次 read/check 后，下一轮即只提供发布 verdict 的工具；同一回合允许的工具动作也受限。

当前例子：

- Ansible `bf98f031`：1009.692 秒结束，剩 490.308 秒（8.17 分钟），有真实失败测试，最终 inconclusive 后保留补丁结束。
- Flipt `3ef34d1`：948.572 秒结束，剩 551.428 秒，首轮超时后审查 inconclusive。
- OpenLibrary `322d7a46`：930.854 秒结束，审查 inconclusive，但正式 eval 成功；这证明 inconclusive 不是失败成绩。
- Webclients `2f2f6c31`：首轮 Reviewer 指出缺少 migrateShares 和启动调用，促成返工，补丁由 7,703 增至 11,782 字符；最终仍 inconclusive 且尚未 eval，不能称为已解决。这是协作确实传递了具体修改反馈的例子。

**修复方向**：把“有明确实现缺陷”“缺少验证证据”“纯基础设施导致无法验证”分开。对于前两种，在剩余预算足够时进行一次有明确目标的返工或验证，而不是一律终止；保持次数上限和补丁/证据无变化时退出，防止空转。只有无具体缺陷时才不编造返工要求。

## 4. 两个已评估失败题的具体原因

### 4.1 Ansible `bf98f031`：扩大修改范围，且放弃已有失败证据

pro19 Ansible case_001 的目标是分开处理值脱敏和键脱敏。最终 eval：F2P **3/4**，P2P **4/5**，基础设施无错误。

- MetaGPT 大幅改写 `remove_values` 的标量/字符串及容器处理，移除了精确匹配时返回 `VALUE_SPECIFIED_IN_NO_LOG_PARAMETER` 的分支，改成 `********`。
- 在约第 718 秒，Engineer 已运行现有 `test_no_log.py`，看到 `test_strings_to_remove` 和 `test_hit_recursion_limit` 失败；后者名称带 recursion，但实际断言失败是哨兵值不同，不能误报为算法仍发生递归溢出。
- Engineer 后续按自己的需求理解写验证脚本，宣布自测通过。Reviewer 的内容也把原测试失败解释为旧契约冲突，声称 patch 实现了新需求；因成功验证证据门槛被改判 inconclusive 后结束。
- 最终 eval 的两项失败与生成期间已知失败相同：`********` 与 `VALUE_SPECIFIED_IN_NO_LOG_PARAMETER` 不一致。

EvoMAS 历史成功补丁的语义差异很明确：保留现有值清洗逻辑，去掉 `remove_values` 对键的处理，增加 `sanitize_keys` 并由 uri 使用；其源代码补丁约 6,765 字符，MetaGPT 为 17,214 字符。补丁大小本身不是正确性依据，关键是原有契约是否被无必要地改变。

**这里也有任务材料冲突**：公开 requirements 写了字符串替换为八个星号、标量保持不变，而当前测试仍要求部分完整匹配返回哨兵。不能把模型遵循文字需求简单描述为毫无依据。可迁移改进是先列出需求与既有行为的冲突，优先最小兼容修改，并让 Reviewer 针对实际失败作明确处置，不把自定义脚本通过当成回归通过。不能把隐藏答案反馈给后续正式生成。

### 4.2 Ansible `bec27fb4`：功能方向相近，具体输出和接口不一致

pro18 smoke 的 ansible-doc 格式化任务：F2P **0/3**，P2P **17/17**，基础设施无错误。

真实失败包括：

1. `tty_ify` 的 italic 输出使用不对称引号（后缀为 apostrophe），测试要求两侧均为反引号。
2. 无元数据描述使用 `[No description available for role test_role]`，测试要求 `UNDOCUMENTED`。
3. 角色 summary 的参数/元数据传递与新契约不一致，导致 entry_points 丢失。

EvoMAS 的历史补丁在 `_build_summary` 中引入 `meta` 参数，使用 `UNDOCUMENTED`，并保留 entry-point 信息。MetaGPT 两次 Reviewer 反馈主要关注旧单测兼容、color 配置及 FQCN 推测，未把上述最终三项差异闭环修正。Reviewer 关于 FQCN 的说法不是这里已确认的 eval 失败原因，不应代替测试证据。

这题体现的是输出细节和接口兼容问题；泛化地再加一次审查或提高输出 token 上限，不能保证解决。

## 5. EvoMAS 历史成功存在答案泄漏：不能直接作为干净基线

以下均在实际任务段的 LLM action/观察中发现，不是仅看到宿主日志记录了 gold patch：

| 实例 | 历史轨迹证据 | 可以确认的事实 |
|---|---|---|
| Ansible `bf98f031` | `logs/run_pro_20260827_065045/ansible.log:224116` 与 `224135` | 对目标提交执行 git show，导出 basic.py/uri.py 修复 diff，随后 git apply；还读取了目标提交中的测试 |
| Ansible `bec27fb4` | 同日志 `216303` 与 `216332` | 直接把 HEAD 与目标提交的源文件差异导出为 pr_src.patch 并应用 |
| Flipt `3ef34d1` | `logs/run_pro_netisol_20260904_023909/flipt.log:58934` | 用 git show 目标提交的三个源文件，重定向覆盖当前工作树；本轮名为断网实验 |
| OpenLibrary `427f1f4e` | `logs/run_pro_netisol_20260904_023909/openlibrary.log:101631`、`103043` | 读取目标提交完整 diff 及目标文件内容，已接触修复答案；不把“读取”夸大成整份代码全部照抄 |

**断网只隔离网络，不删除镜像 Git 对象。** 只删除分支名也不足以阻止按 SHA 读取。当前 MetaGPT 清除了未来对象，所以这些历史运行与当前运行获得的信息不同。以上只能说明抽查的这些成功轨迹受到污染，不能不经全量审计就断言所有 111 个成功均有相同行为。

此外，`output_pro_netisol_failed_rerun/chatdev_ansible/.../results.json` 的 `bf98f031`、`bec27fb4` 同时有 resolved=FULL、eval_error=empty_patch；对应 `.txt` 文件当前为 0 字节。这说明历史成功标签和生成补丁的对应关系需要复核，单凭这些记录无法区分基线测试本来通过、结果合并错配或评估协议问题。本次没有重跑空补丁，不能据此断言是哪一种。

**对照池应重新标注**：每题绑定具体 run、生成 patch SHA、eval patch SHA、评估器版本、base/image ID、网络与 Git 隔离证据；未来提交可见、补丁错配、空补丁异常的成功标记为不可用能力对照。仍可用于离线定位问题，但不把其答案/隐藏测试送给待评估 agent。

## 6. 建议处理顺序

1. **先修调度与漏评**：区分瞬时限流与硬额度失败；对 pro19 现有 4 份补丁仅补 eval，冻结当前预测，不重新采样挑结果。
2. **整理干净对照池**：优先审计目前重放的成功样本。历史 EvoMAS patch 用同一评估器复核只能确认 patch 的通过性，不能消除生成时读答案的污染；能力比较需要在相同隔离条件下重新生成。
3. **修验证闭环与预算分配**：有已知失败测试或验证不足且剩余时间充裕时，增加有明确目标、次数受限的返工/验证步骤；减少无证据的审查结论。
4. **针对性回归**：bf98f031 检查完整匹配哨兵、部分匹配星号、键/值分离和深层容器；bec27fb4 检查 summary 调用契约、默认描述和格式转换。区分公开任务歧义与实现遗漏，不硬编码隐藏答案。
5. **再做同条件基线**：相同模型、预算、任务、断网、base-only Git 下比较单 mini 与 MetaGPT＋mini，独立记录 API 失败、生成失败和 eval 失败。

本次未修改执行器或调度器，也未恢复实验。原始失败/成功/blocked 状态保持不变，便于后续按明确协议处理。
