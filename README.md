# Healthcare Data Platform

合成医疗数据的接入、质量检查、患者纵向档案和临床试验规则匹配平台。数据来自 Synthea CSV，匹配和评测复用已有的确定性规则引擎。结果只说明这批合成数据在当前规则下的表现，不是临床准确率，也不能当作医院系统的验证。

## 能做什么

同一份 SQLite 数据贯穿六步：

1. 把六个 Synthea CSV 当作一个快照导入。
2. 写入患者、就诊、诊断、用药、观察、操作六类规范记录，并保留源行审计。
3. 在总览和数据质量页查看接受数、隔离原因和追溯信息。
4. 按姓名或源患者标识查看纵向档案。
5. 把规范记录交给 `medical-ai-platform` 的规则引擎，得到逐条判定和证据。
6. 用该引擎自带的 103 条合成 Gold 病例做一致性评测。

## 技术栈

- Python
- Streamlit
- SQLAlchemy 2 与 SQLite
- pandas、pydantic

规则引擎和 Gold 数据不在本仓库内，默认读取 `e:\medical-ai-platform`。可用环境变量 `MEDICAL_AI_PLATFORM_ROOT` 改指向。

## 目录

```text
dashboard/app.py                         界面
healthcare_data_platform/ingest/         Synthea 快照检查与入库
healthcare_data_platform/persistence/    表结构与 SQLite
healthcare_data_platform/quality/        质量统计
healthcare_data_platform/patients/       患者搜索与档案
healthcare_data_platform/matching/       规范记录到规则引擎的适配
data/demo_snapshot/                      六文件演示快照
data/platform.sqlite                     本地数据库，由导入生成，不纳入版本库
tests/                                   接入契约与导入测试
```

## 安装与启动

在 `healthcare-data-platform` 目录执行：

```bash
pip install -r requirements.txt
python -m streamlit run dashboard/app.py
```

浏览器打开 http://localhost:8501 。首次启动会在 `data/platform.sqlite` 建库。

## 导入

「数据接入」页需要一个目录，其中同时有：

`patients.csv`、`encounters.csv`、`conditions.csv`、`medications.csv`、`observations.csv`、`procedures.csv`

证据等级填写 `synthetic_synthea`。本机完整数据目录：

`e:\medproject\rwe-project\data\synthea_csv`

仓库内的 `data/demo_snapshot` 是很小的六文件样例，用来确认流程，不代表完整 Synthea 规模。完整观察表导入需要几分钟。

导入使用文件字节的 SHA-256、已固定的表头和时间解析。日期只记为日期，不会补成午夜；带 `Z` 的时间记为 UTC 时刻。源诊断编码、药品名称和检验编码按原文保存，不做术语转换。没有源就诊标识的事件保持为空，不按日期补就诊。同一文件内载荷完全相同的行全部隔离，错误码为 `DUPLICATE_PAYLOAD_IN_FILE`。

批次状态：

| 状态 | 含义 |
|---|---|
| succeeded | 六文件通过，没有隔离行，并登记源文件 |
| partial | 有隔离行。已接受记录可以查询和匹配，但不能作为分析数据集 |
| failed | 快照结构、证据等级或未登记时间格式不通过，不写规范记录 |
| replayed | 与已成功批次的六个文件哈希和证据等级完全相同，只记录重放，不复制规范记录 |

`partial` 或 `failed` 不会因为再次导入同一批字节而变成 `replayed`。

## 页面

- **总览**：六类记录数、接受行、隔离行，以及每个批次自己的计数。上方总数包含库里的全部批次。
- **数据接入**：读取所选目录并写入当前数据库。
- **数据质量**：隔离错误码、最近 200 条隔离明细、悬空引用，以及没有就诊关联的事件。明细包含源文件、源表和源行。
- **患者档案**：按姓名或源患者标识搜索。观察记录每页 500 条，页面写明本页范围和数据库中的总数。
- **临床试验匹配**：选择 Gold 方案中的一条协议，对一名已导入患者执行原规则引擎。
- **评测**：重跑 103 条合成 Gold，显示一致数、不一致数、一致率和免责说明。

患者档案的分页不影响匹配。匹配读取该患者的全部观察记录。

## 匹配时需要知道的限制

- 引擎按原样运行。本平台只负责把规范记录转成它需要的患者档案，并附上源证据。
- Synthea 诊断是 SNOMED。Gold 方案里的诊断规则使用 ICD。非 ICD 编码不会被当作 ICD 比较，对应规则记为证据不足，并保留原编码。
- 检验规则按名称精确匹配，例如 `hba1c`。源检验描述不会被改写成规则里的名称，对不上时记为证据不足。
- 用药规则比较的是源药品名称。中文规则名不会被自动翻译成英文商品名。已有用药但名称不匹配时，引擎判为不符合，并列出参与比较的源名称。
- 没有某类记录时，引擎把该条件记为证据不足，而不是直接判为不符合。

## 测试

```bash
python -m unittest tests.test_synthea_phase_one tests.test_platform_import
```

`tests.test_synthea_phase_one` 覆盖六文件快照、哈希、时间、指纹和重放契约。`tests.test_platform_import` 覆盖入库、隔离、重放，以及非 ICD 诊断和源药品名称进入规则引擎后的证据。
