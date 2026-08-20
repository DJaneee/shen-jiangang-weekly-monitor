# 沈剑刚教授公开信息监测：Windows本地MVP

这是组合A的本地版本：在Windows上采集公开信息、SQLite去重并生成HTML，不发送邮件。

## 当前能力

- PubMed增量检索与XML解析；
- Crossref按ORCID和索引日期进行高精度增量检索；
- 港大官方人物页、Scholars Hub和排名页变更检测；
- 使用ORCID、港大邮箱、单位和姓名进行保守消歧；
- DOI、PMID和标题日期去重；
- 保存原始API响应和网页快照；
- SQLite保存运行记录、候选事件和数据源状态；
- 生成适合手机阅读的本地HTML；
- 不使用邮箱密码，不发送邮件。

Crossref的“本周索引更新”可能对应多年前发表但本周修订元数据的论文。程序将这类记录标为“历史成果本周补录”，不会称为本周发表。

## 首次离线测试

在PowerShell中运行：

```powershell
Set-Location 'C:\Users\46260\Documents\Codex\2026-08-20\new-chat-3\outputs\shen_monitor_local'
.\run_monitor.ps1 -Mode all -OfflineTest
```

测试报告会生成在 `reports` 文件夹。离线测试包含一条已核实论文和一条同名作者候选，用于验证消歧及去重。

## 在线运行

```powershell
.\run_monitor.ps1 -Mode collect
.\run_monitor.ps1 -Mode digest
```

如系统找不到Python，可把Python 3.11或更高版本的路径写入当前用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable('SHEN_MONITOR_PYTHON', 'C:\完整路径\python.exe', 'User')
```

## 数据位置

- `data/monitor.db`：SQLite数据库；
- `archive/raw/`：原始公开页面/API响应；
- `logs/`：运行日志；
- `reports/`：HTML报告。

## 注册定时任务

安装脚本默认设置为每天08:35采集、每周一09:05生成HTML。运行安装脚本会修改Windows任务计划；请在确认时间后再执行：

```powershell
.\install_scheduled_tasks.ps1 -DailyCollectionTime '08:35' -WeeklyDigestDay Monday -WeeklyDigestTime '09:05'
```

该任务使用当前登录用户运行，并设置为错过后补跑和唤醒电脑。当前版本不会发送邮件。

## 当前边界

- 尚未接入商业网页搜索API，因此新闻覆盖主要依赖港大官方来源变更检测；
- 页面发生变化时只记录变更状态，不会自动把整个页面改动认定为新闻；
- 中等置信度候选保存在数据库中，不进入正式HTML；
- 摘要来自公开摘要的前两句，没有调用大语言模型；
- 正式自动化前应进行至少2周试运行。

