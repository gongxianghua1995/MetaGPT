# 最新 SWE 断网实验

新实验入口：[断网基线快速启动指南](../SWE_OFFLINE_QUICKSTART.md)。

整理日期：2026-09-28；代码和精简结果已于 2026-09-29 推送实验分支。清理没有重跑 benchmark，也未改变结果。

| 数据集 | 结果 | 报告 | 精简数据目录 |
|---|---:|---|---|
| Pro test 子集 | 98/216，45.37% | [Pro 报告](../SWE_pro24_experiment_report_20260924.md) | [pro24_full_216_20260922](pro24_full_216_20260922/) |
| Verified test 子集 | 102/154，66.23% | [Verified 报告](../SWE_verified_offline_experiment_report_20260928.md) | [verified_mini_team_offline_154_20260924](verified_mini_team_offline_154_20260924/) |

架构仍是 MetaGPT Leader、mini Engineer、Reviewer 协作，生成和评估容器均按报告中的断网配置执行。额度恢复、控制器恢复和损坏轨迹处理的统计口径以原报告为准。

每个精简数据目录包含模型补丁 `predictions.jsonl`、逐题评估 `evaluations.jsonl`、逐题状态及补丁校验值 `selection.json`、参数和输入溯源、运行源码快照、控制器修订快照及 `export_manifest.json`。导出的 prediction 只含 `instance_id`、`model_name_or_path`、`model_patch`，不混入标准答案或任务测试补丁。

完整轨迹、输入与 usage 保留在 `workspace/` 下的同名批次，详细评估日志保留在 `logs/run_evaluation/<批次名>_*`。这些原始文件由 Git 忽略，不随提交上传。旧报告对照引用的 `workspace/experiment_report_swebench.md` 也保留本地。

旧冒烟、调试目录、较早实验、临时重放脚本和日志已移到：

`/home/xhgong/project_cleanup_archive/20260928_swe_baselines/MetaGPT/`

父目录 `manifest.json` 记录原相对路径、文件数、大小及原因。历史诊断/迁移文档仍保留，其引用的旧运行数据可从归档查阅；复制回原相对路径即可恢复，注意避免覆盖现有文件。

代码入口为 `tests/metagpt/roles/di/run_swe_full_experiment.py`，各生成/评估入口及离线回归测试在同目录。环境准备与协作实现见 [mini 迁移说明](../SWE_mini_backend_migration.md) 和 [迁移经验](../SWE_agent_migration_playbook.md)。`config/pro.json`、`config/ss.json` 是任务划分数据，继续保留；本地提供商配置 `config/config2.yaml` 已停止跟踪并加入忽略，部署时参照 `config/config2.example.yaml` 自行配置。
