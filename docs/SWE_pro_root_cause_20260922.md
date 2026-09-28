# MetaGPT Pro 失败定位：交接信息丢失、审查工具过早关闭与具体实现偏差

范围：pro20 三题冒烟、pro21 OpenLibrary 确认复跑，以及 pro19 同题成功轨迹。本次只定位并补充文档，不改运行代码、不调用模型、不修改实验补丁或评估结果。

## 结论

当前仍有两处可以直接从代码和真实轨迹确认的框架问题：**跨轮交接丢失已确认的测试路径**，以及 **Reviewer 一次读取后就被限制为只能发布结论**。预算虽然增加、补充验证虽然触发了，但接收方未获得完整的已知事实，也缺少纠正错误路径后继续检查的机会。

另有具体实现错误和任务材料冲突。不能把全部失败统一解释为 mini 能力不足，也不能承诺仅修框架就一定通过。修复交接/审查能否提高解决率，仍需单独对照实验。

## 1. P0：已发现的测试路径没有随任务交接

pro21 的 Engineer 首轮已执行并成功返回：

- 第 58 条事件：`ls openlibrary/tests/solr/ && wc -l openlibrary/tests/solr/test_update_work.py`，确认文件存在、885 行。
- 第 60、70、72 条事件：读取该测试文件的不同区间。
- 第 64 条事件：在该文件中搜索待重构接口。

但两个 `review_packet` 都不包含这个正确路径，`mini/submission_2/request.json` 的 task 也不包含它。最后 Reviewer 却执行：

```text
grep ... openlibrary/solr/tests/test_update_work.py
No such file or directory
```

正确路径是 `openlibrary/tests/solr/...`，错误路径把 `tests` 和 `solr` 对调。不是镜像里缺测试：首轮已经读到了。

机制位于：

- [swe_protocol.py:400](../metagpt/roles/di/swe_protocol.py#L400)：普通探索只传 `recent_actions=recent[-2:]`，没有已确认路径/测试命令的持久化字段。
- [swe_protocol.py:185](../metagpt/roles/di/swe_protocol.py#L185)：下一轮只组合旧 Leader 计划、submission packet 和反馈。
- [swe_mini_engineer.py:58](../metagpt/roles/di/swe_mini_engineer.py#L58)：每轮根据这些材料构造新的 worker 请求。
- [swe_mini_worker.py:109](../metagpt/roles/di/swe_mini_worker.py#L109)：每轮新建 `DefaultAgent` 并 `run(task)`，不恢复上一轮对话。

因此共享 Docker 工作区只保证文件延续，**不会自动保留 agent 已经发现的事实**。首轮花钱找到的测试路径会在交接时丢掉。

pro20 也存在错误测试路径；Reviewer 最后甚至据此声称没有可运行的仓库测试。这个结论不能由一次错误路径的失败推出。

## 2. P0：Reviewer 不是在 180 秒内持续检查

[reviewer.py:124](../metagpt/roles/di/reviewer.py#L124) 的条件为：

```python
final = remaining <= verdict_reserve or reads + checks >= 1 or turn == self.max_react_loop - 1
available = [REVIEW_TOOLS[-1]] if final else REVIEW_TOOLS
```

只要前一轮执行过一次 read/check，下一次模型请求就只能调用 `publish_review`。一批可以包含两个动作，但无法根据第一次读取结果再执行第二批检查。

pro21 的真实证据：

- t=1320.365：开始最终审查，工具包含 read、check、publish_review。
- t=1365.907：读取源码。
- t=1366.010：读错测试路径，返回文件不存在。
- 随后的第 268 条请求，**还剩约 132.99 秒，却只提供 publish_review**。
- t=1406.832：返回空 verdict，触发兜底结束。空 verdict 防护已在上轮末尾修复；本节“一次读取后禁用检查”的限制仍存在。

这里仅增加 review_seconds 或 max_tokens 不会恢复 check 工具。应允许“定位 → 执行 → 根据失败进行一次纠正”这种有限检查序列，同时保留最后的结论时间。

## 3. OpenLibrary：删除逻辑存在，但提前查询 Solr 抛错，状态未返回

pro20/pro21 都是 F2P 9/11，失败为 `test_delete`、`test_redirects`。

**更精确的因果链**：

```text
update_keys
  → EditionSolrUpdater.update_key(book)
  → solr_select_work(book_key)
  → requests.get(...)
  → 仓库 conftest.py 的 mock_request 抛出 Warning
  → update_keys 的宽泛 except 捕获并跳过
  → 该 book 的删除状态没有合并进最终结果
```

关键证据是 pro21 [test_output.txt:164](../logs/run_evaluation/run_fe0ff092_openlibrary_001/DeepSeek-V4-Flash-0731/instance_internetarchive__openlibrary-322d7a46cdc965bfabbf9500e98fde098c9d95b2-v13642507b4fc1f8d234172bf8129942da2c2ca26/test_output.txt#L164)：报错明确来自仓库测试的 `mock_request`，消息为 `Network requests are blocked in the testing environment`。pro20 的日志存在相同调用链。

这不是应当恢复联网或安装依赖的问题。测试主动禁止请求；补丁让应当直接形成删除状态的分支依赖了额外 Solr 查询。删除键的 append 写在后面，且局部 state 在异常时没有返回，外层又吞掉错误。

为什么自测没发现：pro20 的自定义 reproduction 用 `WorkSolrUpdater` 验证 `/works/...` 的删除和重定向，却没有覆盖 `EditionSolrUpdater` 对 `/books/...` 的同类输入。Reviewer 将“某一类对象验证通过”推广成了整个需求已覆盖。

与此前 pro19 干净成功补丁对比：成功实现先在 `update_keys` 中统一处理 delete/redirect，加入原键并排队处理重定向目标，再分派其他普通文档。失败实现先按前缀分派，沿用了 edition 分支的旧 Solr 查询逻辑。

这是明确的**分支覆盖缺口及异常吞没**，公开 requirements 本身就要求所有 delete/redirect 文档键进入 deletes；无需把隐藏测试答案写进提示词即可构造通用验证矩阵。

## 4. Ansible bec27：遗漏 helper 签名迁移，不能只归因占位描述

本轮最终 patch 没修改 `_build_summary`，保留：

```python
def _build_summary(self, role, collection, argspec):
```

正式测试调用：

```python
obj._build_summary(role_name, collection_name, meta, argspec)
```

因此两项 summary 测试直接抛出：`TypeError: RoleMixin._build_summary() takes 4 positional arguments but 5 were given`，**还没有执行到占位描述或 entry_points 断言**。见 [test_output.txt:90](../logs/run_evaluation/run_e030b8ac_ansible_001/DeepSeek-V4-Flash-0731/instance_ansible__ansible-bec27fb4c0a40c5f8bbcf26a475704227d65ee73-v30a923fb5c164d6cd18280c02422f75e611e8fb2/test_output.txt#L90)。

另一项失败是 no-color italic 保留旧的前反引号/后单引号，测试要求两侧反引号。

Leader 已将 `_build_summary` 和 metadata 写进计划，Engineer 首轮、返工轮都读过此函数，最终仍未修改；这是需求到实现的覆盖没有闭环。24 项旧仓库测试通过不能证明新增元数据路径已经支持。最终 Reviewer 更多关注颜色和 required marker，发现 metadata fallback 未验证时已经没有返工时间。

公开任务对颜色标记只给了“稳定、明确”的描述，没有直接给出反引号字节级答案；新 helper 的精确参数列表也不能仅由这段泛化描述推出。应区分可从公开需求验证的元数据行为与评估要求的精确内部接口，不能把所有细节都说成毫无依据的低级错误。

## 5. Ansible bf98：公开要求与测试的直接冲突

公开 requirements 明确要求 `remove_values` 对值中每次匹配替换成 `********`，另行限定“完整匹配返回 VALUE_SPECIFIED_IN_NO_LOG_PARAMETER”用于 mapping key。

正式失败测试却要求完整匹配的 value 仍返回旧哨兵。Leader 按公开文字规划，Engineer 实现八个星号，Reviewer 也明确说明旧测试与文字要求冲突。两次相关断言仍失败。

该题应标注为“公开材料/评估契约冲突”，保留其正式未解决成绩。不能靠反复重跑同一提示或加大 token 假装能消除冲突，也不能把诊断后获知的隐藏期望反灌给正式生成。

## 6. 时间与 token：不是本轮的直接截断原因

4 次新采样的首个文件变化在合并轨迹中约为 571、723、740、763 秒；此后首轮约 900 秒提交。探索消耗了大部分首轮时间，初次修改后可用验证窗口很短。

这些是合并轨迹记录的时间，worker 启动/导入开销未被精确校准，不能当成毫秒级墙钟指标。可确认的是各次均先长时间阅读，后期才修改，随后由阶段期限结束。

已返回响应的 finish_reason 均为 tool_calls，没有观察到 length 截断。模型确实有 reasoning 用量；当前证据不支持“完全没思考”或“16K 单次输出上限直接截断造成这几项失败”。被阶段 deadline 中断、没有返回 usage 的请求另计，不能由已返回响应排除其影响。

## 7. 建议修复顺序与验证边界

1. **保留已确认事实**：测试路径、工作目录、可运行命令、公开接口、已知冲突进入持久化 handoff。测试路径必须来自真实文件发现，不能只复制 Leader 的猜测。
2. **放开有限的多步审查**：在 deadline 与保留结论时间内允许 read→check→一次纠正；不要以 reads+checks≥1 关闭所有检查工具。
3. **要求行为覆盖表**：例如 works/authors/books × normal/delete/redirect，或 role metadata 有/无 × argspec 有/无；明确哪些组合未验证。表应从公开需求构建。
4. **早点完成最小实现和验证**：减少无进展重复读取，为局部测试留下实际时间；先保证接口存在，再做 import/smoke。是否调整首轮比例应单独做对照。
5. **冲突案例单独审计**：保留正式计分，同时区分任务材料冲突、接口细节缺失、正常实现错误；不注入隐藏答案。

本次证据可以确认机制及失败调用链，但没有实施以上下一轮修改，也没有执行修复后的 A/B，因此不预报成功率提升。
