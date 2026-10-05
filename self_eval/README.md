# Praxis 小型自建任务自动评测

20道本地基础任务，每类5道：命令、文件、数据处理、多步工作流。无需Docker，无资源下载；调用项目真实Runtime和普通本地工具，保留Windows命令沙箱。不是公开benchmark或官方排行榜。不要把这些基础任务的分数推广为生产场景表现。

## 怎么运行

从 `D:\project\Praxis-main` 运行，使用项目已安装的Python环境：

```powershell
cd D:\project\Praxis-main
# 只查看任务：不调用模型
.\.venv\Scripts\python.exe -m self_eval.run --list
# 只生成20题练习文件和题目说明：不调用模型、不生成成绩
.\.venv\Scripts\python.exe -m self_eval.run --prepare
```

真实评测请用**管理员PowerShell**，沿用之前已通过的Windows Broker、权限审计及磁盘预留条件。不要为了测评禁用审计或允许宿主机降级。后台网页不需要启动；脚本直接使用Runtime。已有模型配置从指定数据目录的SQLite只读加载，密钥只在内存使用，不复制到评测文件。不更改原应用配置或聊天历史。

```powershell
# 检查模型身份、管理员身份及Broker状态，不调用模型
.\.venv\Scripts\python.exe -m self_eval.run --preflight --data-root D:\PraxisData

# 先用3题各跑1次估计费用及耗时（真实模型调用，可能收费）
.\.venv\Scripts\python.exe -m self_eval.run --run --allow-model-api --data-root D:\PraxisData --task cmd-01 --task file-01 --task data-01 --repeat 1

# 确认试跑正常后，正式20题各3次，共60次
.\.venv\Scripts\python.exe -m self_eval.run --run --allow-model-api --data-root D:\PraxisData --repeat 3
```

如果网页使用的实际数据目录不是 `D:\PraxisData`，修改 `--data-root`，不要猜目录或重新配置密钥。需要代理时，沿用你正常模型调用终端的代理环境；脚本不修改系统代理、Git或Docker设置。

默认输出到 `D:\PraxisSelfEval\run-时间-随机编号`。每次运行创建新目录，保留成果以便核查，不自动删除文件。也可指定 `--output D:\其他专用测试目录`。

## 自动完成什么

1. 固定20题和题目摘要。前两题/每类标为dev（共8题），另12题标为holdout。
2. 每次尝试创建独立工作目录、客户端数据目录、新会话和原始测试文件；不用清理旧目录。
3. 复用正常Agent Runtime；只开放read_file、glob、grep、file_operation、run_command、write_stdin。关闭memory、skills、MCP、subagents，不宣称测量这些能力。
4. 自动批准专用测试目录内的正常工具操作；拒绝沙箱外提权重试、外部信任和交互提问，不绕过命令沙箱。
5. 使用JSON、CSV、文本、旧路径消失、输入文件字节不变等确定性检查。输出JSON严格匹配要求字段和数字类型。只有成果通过、真实工具成功执行、Runtime正常完成才通过；命令题还要求命令退出码为0。
6. 每题完成立即保存结果和更新总报告；用户Ctrl+C后保留部分结果。连续两次环境/模型运行故障则停止，避免付费空跑。

开发/留出分组是使用约定，不保证你或模型从未看过题目。可用 `--split dev` 调试，再冻结代码和参数、用 `--split holdout` 评测；在留出题上调参后，就不能继续声称它是未参与调参的测试集。默认all用于一次性基础功能测量。

## 运行预算与费用

默认串行执行；每题工具上限20、模型请求预算16、Agent协作式超时300秒；每次响应最多2048输出Token（若原配置更低，保留较低值），温度保留原配置；transport最多重试1次。单个模型请求未返回时，协作式超时并非精确硬时限。预算在报告中记录，指标只适用于这些参数。

输出Token上限不是总费用上限：每轮模型会重复读取系统提示与历史，输入Token也收费，transport重试也可能产生费用。不能在不知道供应商费率的情况下给出可靠金额；建议3题试跑后根据用量估算。

可配置 `--max-tool-calls`、`--max-model-calls`、`--timeout-seconds`、`--max-output-tokens`；改变预算后需作为另一组实验，不能混合比较。

## 报告文件

- `questions.md`：本次题目，不包括参考答案。
- `report.json`：模型身份（不含地址或密钥）、参数、代码/题目摘要、每次结果、失败类型、指标和验收条件。
- `report.md`：可读的逐题成绩。
- `resume_metrics.md`：仅完整跑完所选实验后生成基于真实数据的简历表述；未完成则明确提示。
- `attempts/<任务>-<次数>/workspace`：原始输入和实际成果。不会把参考答案放进去。
- `attempts/<任务>-<次数>/client`：本次独立客户端状态。
- `attempts/<任务>-<次数>/events.json`、`result.json`：裁剪脱敏的事件证据和单次结果。不是完整推理日志。

成功率=通过次数/已执行次数，分母包括环境、模型及验收失败。中止后的报告显示完成数量，不能把部分报告当完整60次结果。耗时/工具次数平均值只统计成功任务；同时保留失败任务的真实耗时与调用记录。工具次数按call_id去重，避免同一命令等待资源的重复通知抬高次数，另保留原Runtime事件计数tool_call_events。异常发生在Agent启动前时，Agent耗时是null，不伪造为0。

Token只有每次model_response都报告有效usage时才作为完整值；否则total_tokens为null，同时保存reported_token_subtotal作为已知部分。报告token_coverage，缺失值不当成零，不将usage等同于供应商账单。

每次正式运行都生成独立成绩；不会把开发试跑、单元测试或模拟模型的结果混进正式60次成绩。选择部分任务时，简历中的题数按实际数量生成，不冒充20题；单类别测试不要套用全四类覆盖表述。

## 不调用付费模型的验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_self_eval.py --basetemp D:\PraxisSelfEvalTests\unique-test-directory
.\.venv\Scripts\ruff.exe check self_eval tests/test_self_eval.py
```

测试包含全部20题的正反验收、输入保护、路径边界、缺失Token、部分报告，以及loopback本地模拟模型对真实Runtime/文件工具的接入。模拟模型只验证评测工具能工作，不是Agent能力成绩。测试不启动真实命令沙箱、不改系统审计、不调用付费API。
