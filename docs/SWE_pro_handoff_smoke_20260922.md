# MetaGPT SWE Pro：交接证据与连续审查修复冒烟

## 目的与范围

针对 `SWE_pro_root_cause_20260922.md` 中的两项框架问题：已读测试路径在交接时丢失；Reviewer 一次读取后立即失去检查工具。此次保留 MetaGPT Leader + mini-swe-agent Engineer + MetaGPT Reviewer 架构，不改模型、不增加总预算，也不向生成过程提供历史答案或隐藏评估信息。

## 修改

1. `swe_protocol.py`：从历史执行事件提取成功读取的实际文件路径，连同命令、时间和树版本放入 `repository_facts.observed_files`。仅接受保守识别的普通读取命令；失败读取、管道、重定向、脚本和猜测不构成路径证据。最多保留 32 项，测试路径优先。路径曾存在不等于测试通过，树变化后仍需复核。
2. 提交包向 Reviewer 传递这些事实；mini 新一轮启动时重新汇总，包含上一轮 Reviewer 新找到的文件，避免只拿到旧 Leader 计划和最后两次读取。mini 仍使用上游实现，每轮新建实例，共享工作树与显式交接证据。
3. `reviewer.py`：最多 4 次读取/检查、其中最多 2 次测试，可分多个模型回合执行。去掉“一次探索后强制结论”的限制；最多 6 个模型回合，保留结论回合。180 秒预算内预留 60 秒给结论；探索超时后强制收尾。总审查预算不变。
4. Reviewer 提示要求优先使用已有路径，并区分公共需求中的不同输入类型和操作，不能把一个分支的测试通过推断成所有分支都覆盖。原有失败证据、修改测试阻止批准、一次验证回访等机制保持有效。

## 验证

- 72 项离线回归全部通过，运行记录 `/tmp/metagpt_pro22_tests.log`。
- 新增回归覆盖：早期测试路径经历 40 次后续读取仍保留；失败/猜测路径不当作事实；Reviewer 的新发现传给下一轮 mini；读错路径后可纠正并执行测试；动作上限和超时仍保留结论机会。
- 上一版 Pro21 OpenLibrary 轨迹离线回放：两次提交均能保留 `openlibrary/tests/solr/test_update_work.py`。此回放仅检验框架提取逻辑，未向新实验注入旧轨迹。

## 真实冒烟设置

- 输出：`workspace/pro22_handoff_smoke_20260922`，每题保留输入哈希、代码快照、生成轨迹、patch 和正式评估结果。
- 并行两域：OpenLibrary `322d`、Ansible `bec27`。这是针对已知失败的诊断性复跑，不代表全量成功率。已知存在公共规格与测试冲突的 Ansible `bf98` 不作为本次修复验证对象。
- 每题 25 分钟；Leader / Reviewer 各 180 秒；各角色单次输出上限 16,384 tokens；Engineer 全题最多 80 次请求；首次编辑截止为总预算 60%。模型仍为 `DeepSeek-V4-Flash-0731`。
- 生成和评估容器均 `network=none`；生成仓库仅保留基线 Git 对象。模型 API 由宿主调用，任务容器不联网。

## 结果

两题均完成生成及正式评估，结果 **1/2 resolved**。这是两个定向样本，不能据此推断全量成功率，也不能把单次随机生成改善全部归因于框架修改。

| 用例 | Pro20 → 本轮 FAIL_TO_PASS | 本轮 PASS_TO_PASS | 正式结果 | 生成耗时 | 含启动与评估的墙钟耗时 | 最终 patch 字符 |
| --- | --- | --- | --- | --- | --- | --- |
| OpenLibrary `322d` | 9/11 → **11/11** | 0/0 | **resolved** | 1,500.16 秒 | 1,546.48 秒（25.77 分） | 30,497 |
| Ansible `bec27` | 0/3 → **0/3** | 17/17 | unresolved | 1,442.14 秒 | 1,494.64 秒（24.91 分） | 8,423 |

两题并行，整批约 25.77 分钟；控制器均正常退出，patch 均成功应用，正式评估均无基础设施故障。生成与评估期间运行代码哈希保持一致。

| 用例 | 输入 tokens | 输出 tokens | 总 tokens | 已报告 reasoning tokens（已计入输出） | 缓存输入 tokens（已计入输入） |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenLibrary | 1,110,030 | 62,124 | 1,172,154 | 46,342 | 856,832 |
| Ansible | 1,561,912 | 49,089 | 1,611,001 | 38,488 | 1,292,288 |
| 合计 | 2,671,942 | 111,213 | **2,783,155** | 84,830 | 2,149,120 |

仅累计合并轨迹的 `model_usage`，不再累计重复的 `role_model_response` 或 mini 原始事件。未返回 usage 的中断请求可能另有消耗，上述不是账单总额。没有观测到额度不足或限流；OpenLibrary 最终 Reviewer 请求因总时限取消，记录了一次 `CancelledError`，不属于 API 额度故障。

## 轨迹证明了什么

### 交接和连续检查修复生效

- 两题两次提交的 `repository_facts` 都含正确测试路径；第二轮 `mini/submission_2/request.json` 也包含该路径。
- OpenLibrary 第一轮审查在约 938 秒读取后，于 975–977 秒继续读取和执行 API 检查；第二轮于 1,346 秒读取后，于 1,439–1,440 秒继续读取并执行正确路径的仓库测试。
- Ansible 第一轮在约 922 秒读取后，于 956–957 秒继续读取并运行 `test/units/cli/test_doc.py`，24 项旧测试通过。Reviewer 仍指出需求缺失并要求修改，没有以旧测试通过代替新行为覆盖。
- Ansible 第二轮继续多回合检查，复现 `_dump_yaml` 的 `NameError` 并给出具体反馈。两题均没有 `review_error` 或空 verdict 异常。

### OpenLibrary：正式失败分支已修复

本轮 patch 在 `update_keys` 的统一入口先处理 delete/redirect，把原 key 加入 deletes，并把 redirect 目标加入待处理队列，随后才分派普通记录。上两轮失败的 `test_delete`、`test_redirects` 本轮都通过，全部 11 项 FAIL_TO_PASS 通过。

不过最终 Reviewer 的结论请求从约 1,440 秒持续到总预算结束，未产出第二次 verdict；最终结束原因是 `wall time budget exhausted`。框架保留并评估 patch，因此此处的成功以正式 eval 为准，不能写成 Reviewer 已批准。60 秒结论预留仍不能保证长推理模型按时返回。

### Ansible：协作识别问题，实施仍未完成

正式失败仍是：

1. `_build_summary(role, collection, meta, argspec)` 的两个评估调用触发 `TypeError: ... takes 4 positional arguments but 5 were given`；不能误写成已经执行到占位文案断言后失败。
2. `I(italic)` 的关闭符号仍输出单引号，评估要求反引号。

此外，第二轮 patch 在 `_dump_yaml` 注册 `AnsibleUnicode` 表示器时没有导入该符号。本地 reproduction 和最终 Reviewer 都确认 `NameError`；它是模型补丁引入的缺陷，不是缺少容器依赖。该新增缺陷并非上述三个正式失败条目的直接原因，需分别记录。

首个仓库修改直到约 **837.75 秒（13.96 分钟）**才出现；OpenLibrary 为约 626.41 秒。Ansible 首轮花了大量时间搜索和试验，Reviewer 到约 1,024 秒才指出实现不完整；第二轮修补后，新缺陷直到最终审查才被确认，而 Engineer 编辑截止为 1,320 秒。最终结束原因为 `insufficient editing time for requested changes; patch preserved`。

## 剩余问题与下一步

此次两项框架修复已经得到回归和真实轨迹支持，但尚未解决执行节奏与需求覆盖：

- 优先让 Engineer 更早实现可验证的最小修改，并为新增代码的导入、真实入口调用保留即时检查时间；只提醒“尽早编辑”未阻止本轮近 14 分钟的首改延迟。
- 将公共需求逐项映射到实现位置与验证证据，特别区分无色/彩色输出、角色元数据、空参数等分支。Reviewer 能看出缺项，Engineer 仍可能优先修外围细节。
- 考虑压缩反馈到必须修复的具体事项，并安排更早的中途审查；本轮最后阶段发现一个简单 NameError 也没有修正窗口。
- 结论请求仍可能超过 60 秒。若继续调整，应单独验证结论输出长度/请求时限及取消后的结构化收尾，不应把审查超时计成 API 额度问题。
- Ansible 精确内部接口预期在公开任务中并不完整，不应把隐藏测试签名注入生成提示来“修复”框架；继续以公开需求和本地证据做通用适配。

## 复核入口

- 机器可读指标：`docs/SWE_pro_handoff_smoke_20260922.json`，含 token 分角色统计、源码哈希、原始报告位置及前一轮对照。
- 启动参数、输入哈希与完成时间：`workspace/pro22_handoff_smoke_20260922/selection.json`、`completion.json`。
- 各题目录下 `run/<domain>/case_001` 包含 `all_preds.jsonl`、`reports`、`traces/<instance_id>/events.jsonl` 及各轮 mini 请求和事件。
- 正式评估 run ID：OpenLibrary `run_dce15e44_openlibrary_001`；Ansible `run_957e57b7_ansible_001`。详细断言和测试输出位于 `logs/run_evaluation/<run_id>/<model>/<instance_id>/`。
