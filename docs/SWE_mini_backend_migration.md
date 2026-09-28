# SWE 执行器迁移与对照实验

跨项目复用时，先看 [SWE agent 迁移指南：改动、踩坑与验收顺序](SWE_agent_migration_playbook.md)。本文保留具体配置和迭代记录，迁移指南按执行、协作、验证、预算和实验可信度整理经验。

全量四域运行已于 2026-09-18 启动：216 题（Ansible 63、Flipt 54、OpenLibrary 60、WebClients 39），四域并行，最多两个 eval 并行。生成与 eval 容器均断网并检查实际网络模式，生成侧保留基准 Git 对象隔离；运行说明与实时进度见 [全量运行目录](../workspace/pro10_mini_team_full_20260918_v3/README.md)。新增全量控制器支持断点续跑、源码指纹校验、独立错误分类，49 项测试及四域真实 Docker 检查通过。WebClients 的 BusyBox timeout 兼容问题已修复；初始失败启动单独归档，不计入该目录的成绩。

**最新真实复跑为 pro9：新预算下仍为 2/3 通过，三题均非空补丁，Reviewer 无输出截断或审查超时。** MARC 的审查用时 63 秒，最终输出 1794 tokens，顺利给出批准；gRPC 仍因评估测试引用缺失私有符号而失败。MARC/gRPC 都耗尽首次编码阶段后由 runner 收集补丁，主动收尾仍待改善。详情见 [pro9 新预算复跑分析](../workspace/pro9_mini_team_budget_20260918/analysis.md)。

此前 pro8 隔离历史复跑为 **2/3 通过、三题均非空补丁**，41 项回归测试通过。两题 Reviewer 批准，一题审查超时后保留补丁；gRPC 仍在独立评估中失败。结果与剩余问题见 [pro8 分析](../workspace/pro8_mini_team_clean_20260918/analysis.md)。pro7 原始 3/3 因读取未来 Git 历史而作废；当前分数不证明协作优于单 mini。

## 当前预算配置（pro8 后更新）

预算默认值统一在 [swe_budget.py](../metagpt/roles/di/swe_budget.py)。两个生成入口都支持独立角色参数，协调角色不再受 Engineer 配置或旧 1536 上限约束。以下参数用于 `run_swe_backend_comparison.py`：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `--minutes` | 20 | 整个任务的执行时间上限，含全部团队阶段 |
| `--max-tokens` | 16384 | Engineer 每次调用的输出 token 上限 |
| `--leader-max-tokens` | 16384 | Leader 每次调用的输出 token 上限 |
| `--reviewer-max-tokens` | 16384 | Reviewer 每次调用的输出 token 上限 |
| `--leader-seconds` | 180 | Leader 整个规划阶段上限 |
| `--reviewer-seconds` | 180 | 每次审查的阶段上限 |
| `--first-edit-fraction` | 0.6 | 首次提交截止时间占任务总时长的比例，包含前面的规划时间 |

直接调用 `run_swe_agent_for_benchmark.py` 时，总时间参数名为 `--max_wait_time_per_case`（同样默认 20 分钟），Engineer 输出参数名为 `--model-max-tokens`（未指定时使用原模型配置），其余五个角色/阶段参数相同。

默认最迟时间线：Leader 0–3 分钟，首次编码到第 12 分钟，首次审查到第 15 分钟，返工到第 17 分钟，最后审查到第 20 分钟。阶段可提前结束；这不是强制等待或平均耗时承诺。配置校验要求首次编码至少留 60 秒，首次提交后至少保留两次审查与 60 秒返工；不合理组合在启动容器前报错，不会悄悄缩短角色时间。

Leader 和 Reviewer 将阶段预算的一半留给最终计划/判定，默认 90 秒；读取/检查的单命令上限随阶段预算增加，默认最多 60 秒，且不能侵占最终输出保留时间。模型调用的 HTTP 超时同步使用该调用剩余预算，外层阶段与任务总截止时间仍生效。

这里增加的是包含服务端所报告 reasoning token 的输出额度和允许调用耗时；没有设置模型专属 `reasoning_effort` 或额外独立思考 token 参数。MARC 旧记录中 1536 个 completion token 全部记为 reasoning，说明旧上限会在最终判定前截断思考。pro9 已观察到该题在新预算下完成审查，但通过数不变；全量效果仍需后续结果确认。

此次配置修改后 **46 项回归测试通过**，覆盖不同角色 token 独立传至模型接口、HTTP 超时传递、两个 runner 的配置往返、阶段时间分配、无效组合拒绝，以及探索超时后保留最终判定机会。已用新预算完成 pro9 真实复跑；pro8 结果仍对应旧预算，两个目录的历史数据分别保留。

本次先修正 MetaGPT 的信息传递，再接入已安装的 mini-swe-agent，并用小样本对照检验。结果与分析见 `workspace/pro5_mini_comparison_20260918/analysis.md`。单 mini 原始评分 2/3，原生单 Engineer 1/3，团队加 mini 0/3；有超时与回收异常，属于诊断结果。当前以 MetaGPT＋mini 三角色团队为待验证基线，单 mini 用作消融对照；详审见 `workspace/pro5_mini_comparison_20260918/team_failure_analysis.md`。

修复后仅复跑团队组：**2/3 通过，三题均非空补丁**。详情见 `workspace/pro6_mini_team_20260918/analysis.md`。后两题 Reviewer 仍超时，Leader 计划也仍有不足；本轮结果证明部分执行问题已缓解，不等同于协作增益已验证。

## 本轮协作协议更新（pro7）

本轮在 `workspace/pro7_mini_team_20260918` 复跑同三个任务。Leader 和 Reviewer 保留 MetaGPT 角色/消息路由，改用 SWE 专用原生函数调用循环；mini 编码仍使用安装包里的 DefaultAgent。Coordinator 每次输出上限 1536，Engineer 仍为 16384；总 case 预算、首次编码截止和每次审查时长不变。

Leader 所有探索出口统一进入 `publish_plan`，要求 locations/hypothesis/steps/verification/unknowns。批量调用触顶后不再直接发送原始观察。无法生成计划时明确记录 planning_incomplete。

Reviewer 只接收一次公开任务和当前提交快照，不进入语言检测或通用规划。read/check/publish_review 原生工具限制检查数量并为最终判定预留时间。审查状态分 approved、changes_requested、inconclusive；后者直接保留补丁结束，不向 Engineer 编造修改要求。预算不足以及补丁/检查证据未变时阻止重复提交。

验证端提供 `swe-check`：先保存真实命令退出码，再显示限定长度日志。共享 shell 使用 pipefail；零测试、失败输出、命令超时分别记录为证据状态，不自动等同正确性判断。提交汇总保留历史检查和对应 tree hash，优先保留失败/零测试记录，减少重复 diff 内容。

新增 `role_model_request/response/interrupted` 记录协调角色实际请求、响应、耗时与取消；mini 逐次模型异常只记录异常类型。角色使用非流式工具接口，以获取正常 usage；没有独立修补通用 OpenAI 流式 provider。

## 修复的确定性问题

原生 `swe_protocol.request_messages()` 原先只包含任务、工具历史和审查反馈。Leader 发给 Alex 的分析会触发角色运行，却未进入实际模型请求。现在成功发送的交接内容保存在共享 `SWECaseState.handoffs`，原生与 mini 执行器均显式读取；这份内容独立于角色的短期记忆裁剪。审查反馈也持久保存。

`events.jsonl` 记录公开任务输入、实际 Engineer 模型请求、各角色模型用量、仓库内容变化、提交、审查启动和审查结论。工具命令和观察仍成对保留。`verification_command` 只是按命令识别的候选验证动作：自写脚本可能未匹配，命令成功也不等于任务通过，最终以统一评估器结果为准。

两种 Engineer 使用同一份剩余时间计算。历史 pro6–pro8 团队复跑将首次编码截止设为总预算的 70%，Leader 规划最多 60 秒，每次 Reviewer 最多 45 秒，返工截止为任务结束前 45 秒。对于 600 秒任务，首次提交最迟约 420 秒、首次审查最迟约 465 秒，预期至少有 90 秒返工；取消和补丁采集开销仍计入总时长，实际值应读轨迹。当前默认已更新，见本文顶部预算配置；历史结果不随默认值变更。

## mini 接入边界

`SWEMiniEngineer` 仍是 MetaGPT 角色。Leader、Reviewer、消息投递、共享容器、提交编号和返工状态机继续使用 MetaGPT 实现。编码阶段启动独立 Python 进程，运行安装包中的 `minisweagent.agents.default.DefaultAgent` 和 `LitellmModel`。没有重新实现同名 agent 循环。

独立进程用于兼容当前环境：MetaGPT 使用 Python 3.9，mini 使用 Python 3.11。`--mini-python` 指定安装 mini 的解释器；当前版本是 2.4.6。工作流模板取自该安装包的 `config/benchmarks/swebench.yaml`；修改工作目录、断网说明、新文件 diff 提示及时间预算说明，并在工具观察后附上剩余阶段时间与实施提醒。当前安装包可能包含此前 EvoMAS 的本地适配，不能将它称为未修改的上游版本。

DockerTerminal 与 mini 共用无 MetaGPT 依赖的 `swe_shell.docker_shell_argv()`：`bash -c`、`BASH_ENV=/root/.bashrc`、容器内 timeout。直接执行原命令，不拼接会破坏 heredoc 的完成标记；DockerTerminal 保留退出码和超时状态，记录 Leader/Reviewer 命令及输出。

mini 每条命令通过 `docker exec` 在现有任务容器内开一个新 shell。模型请求在宿主机发送；容器沿用生成 runner 的 `--network none`。API 凭据通过进程环境传递，不写入请求文件或模型配置轨迹。上游 mini 的完整轨迹保存在 `traces/<instance>/mini/submission_N/trajectory.json`。

mini 完成、达到步数上限或阶段超时后，适配器通过独立 Docker exec 收集 patch，再调用 MetaGPT 的显式提交协议，把实际共享工作树交给 Reviewer。阶段超时会标注为超时，不等同于修复成功。`patch.txt` 是 mini 的补丁传输文件，在三组任务中均被排除于 Git 收集之外。外层取消会终止并回收 worker；最终 diff 由 benchmark runner 在清理容器前通过独立 Docker exec 采集，不复用已取消的 shell 输出流。初始镜像中未被 agent 改动的文件级差异会被排除；若这些文件被 agent 改动，则保留完整 diff。

每次提交保存 `review_packet`，包含实际补丁（过长时截断并明确提示）、结束原因、按命令去重后选取的历史检查证据，以及最近两条非 diff 动作；Reviewer 与返工 mini 都显式接收。Reviewer 对实际空补丁立即退回；非空补丁的审查结果分 approved、changes_requested、inconclusive，超时归入 inconclusive 并保留当前补丁结束。Leader 不再把检索中出现的路径自动标为必改文件，改为提交观察支持的结构化计划。

## 对照设计与可重复运行

三组为：`native_single`（原生单 Engineer）、`mini_single`（单 mini）、`mini_mas`（MetaGPT Leader + mini Engineer + Reviewer）。

同一任务的公开 issue、requirements、interface 和初始容器镜像一致。相同模型、temperature、Engineer 单次输出上限及整组 wall-clock 预算；Leader/Reviewer 输出上限独立配置。总预算包含 Leader 与 Reviewer，初始化与最终评估不计入。各角色 token 实际用量另外记录，并非固定总 token 预算。native 与 mini 的工具协议、系统提示和历史管理本来就不同，第一组与第二组比较的是整个编码执行器；第二组与第三组比较加入协作后的净效果。

输入 JSONL 保留评估所需的原始元数据，但 runner 只把公开任务字段拼入模型请求，隐藏测试和参考补丁留给评估器。所有新补丁使用同一个 `run_swebench_pro_eval.py` 评估。此次使用 3 个历史 EvoMAS 报告通过、MetaGPT 未通过的诊断样本；历史 EvoMAS 分数仅用于选样，不能和本轮评分直接混算。样本不是随机抽取，不能外推总体提升幅度。

```bash
/home/xhgong/miniconda/envs/metagpt/bin/python \
  tests/metagpt/roles/di/run_swe_backend_comparison.py \
  --instances-file workspace/pro5_mini_comparison_20260918/instances.jsonl \
  --output workspace/your_new_run \
  --mini-python /home/xhgong/miniconda/envs/evomas/bin/python \
  --eval-python /home/xhgong/miniconda/envs/evomas/bin/python \
  --minutes 20 --max-tokens 16384 \
  --leader-max-tokens 16384 --reviewer-max-tokens 16384 \
  --leader-seconds 180 --reviewer-seconds 180 \
  --first-edit-fraction 0.6 --workers 3
```

仅复跑目标团队组时追加 `--arms mini_mas`。新预算应使用全新输出目录，保留历史运行的 manifest 和评分。

输出目录应使用新的名称。`manifest.json` 保存参数、实例与源码摘要；每个子任务保存生成日志、轨迹、预测补丁、评估日志和报告。进度读取：

```bash
python3 tests/metagpt/roles/di/summarize_swe_backend_comparison.py workspace/your_new_run
```

## 已完成的验证

- 49 项回归测试：既有协议与评估检查，以及 Leader 交接、原生审查循环、独立预算与超时传递、检查证据、重复提交防护、mini 输入和提交、超时取消、Git 对象及子模块隔离、全量调度与恢复。最终独立补丁采集路径的 BusyBox 修复还复核了相关 11 项测试和四域真实 Docker 采集。
- 真实 mini 集成检查：本地模拟模型经 LiteLLM 返回三轮工具调用，在独立断网 Docker 容器内创建新文件、生成补丁并得到 `Submitted`；验证修改事件、模型用量和轨迹不含 API key。此检查不使用真实模型、也不证明解题能力。

测试命令：

```bash
/home/xhgong/miniconda/envs/metagpt/bin/python -m unittest \
  tests.metagpt.roles.di.test_swe_revision_flow \
  tests.metagpt.roles.di.test_swe_mini_integration \
  tests.metagpt.roles.di.test_swe_patch_recovery \
  tests.metagpt.roles.di.test_swe_team_runtime \
  tests.metagpt.roles.di.test_swe_native_team \
  tests.metagpt.roles.di.test_swe_budget \
  tests.metagpt.roles.di.test_swe_full_experiment
```


## 生成环境的 Git 历史边界

pro7 中检测到 agent 从镜像的未来 Git 历史读取目标源码与测试，因此该轮原始 3/3 不能作为有效基线。生成 runner 现通过 `swe_repository.isolate_history_command` 为主仓库及已初始化子模块建立只含当前基准提交的浅对象库，保留 SHA 与工作树，并移除原对象库。仅删除 refs 不足以防止按 SHA 读取未来提交。

子模块由最深层开始转换成独立浅仓库，随后替换主仓库 `.git`，避免原 `.git/modules` 消失后模块失效。评估容器不采用这个限制，以便加载 benchmark 测试。初始化失败会在模型调用前终止，不能记作 agent 解题失败。

干净运行在 `workspace/pro8_mini_team_clean_20260918`；OpenLibrary 两题初始化曾因旧版子模块处理失败，修复后的有效尝试单独保存在其 `retry_submodules` 下，最终汇总使用 `final_results.json`。本轮最终代码共 41 项回归测试通过，包含未来对象不可读、原始 SHA/脏工作区/子模块保留，以及真实 Docker 检查。
