# 开发与部署文档

版本：2026.11.02 · 报送进度与回执体验优化版。本文对应当前运行代码；操作步骤见 [操作说明](usage.html)，验证范围见 [版本验证记录](verification.html)。

## 1. 产品边界与架构

网站用于员工主动报送作品、管理员审核归属和查看每日完成情况。员工免网站账号登录，但必须完成至少一个抖音账号核验。未完成报送不等于没有在抖音发布。自动巡检、历史补查和老板端采集任务页已移除，不承诺自动发现未报送的平台作品。

前端为原生 HTML/CSS/JavaScript，无需前端编译；业务 HTTP、SQLite、CSV/XLSX 导出主要使用 Python 标准库。TikTokDownloader 作为相邻项目运行，由 enhanced_api.py 适配。运营报送使用上游已有的 /douyin/share 和 /douyin/detail，没有另造平台数据接口，也不依赖其自带 Web UI。

- 业务服务 8877：/report 员工入口、/admin 管理入口、/help 文档，使用业务数据库。
- 独立工具 8879：仅服务器本机使用，保留下载、评论等原有工具功能，使用独立数据库和配置。
- 本机上游 API 5555：负责平台读取；接口服务可连接不代表平台采集一定成功。

上游作者已说明不再维护加密参数算法，原 Web UI 尚未重新开放。平台变化、账号授权或字段缺失会导致失败；扫码核验和填写 Cookie 都不是平台可用性的保证。

## 2. 安装条件与首次启动

交接入口：部署人员运行 `setup-shortcut.ps1`（或“创建老板桌面入口.cmd”），创建当前用户桌面的“DouK 老板工作台”。快捷方式调用 `open-workspace.ps1`，复用 start.ps1 并检查业务服务标识，再打开 /admin。失败记录 data/launcher-error.log 并显示中文提示；不修改启动任务或防火墙。支持中文/空格路径，重复创建前备份旧 .lnk 并校验 SHA256。移动项目后需重新创建入口、重新配置登录启动。

源码包不包含解释器、Node.js、Chrome 或现场运行数据，首次需要维护人员安装环境。给老板的逐步交接清单位于根目录“老板交付说明.md”。

需要 Windows 10/11、Python 3.12+、Node.js 18+；建议使用维护中的版本。员工抖音扫码核验需要服务器安装 Google Chrome；仅独立工具直播录制需要 FFmpeg。首次安装依赖需要联网。

1. 解压到固定目录，例如 D:\DouK，保持 DouK-Web 与 TikTokDownloader 相邻；不要在 ZIP 内运行，也不要搬用其他电脑的 .venv-api。
2. 运行 DouK-Web 中的“安装环境.cmd”。install.ps1 创建本机虚拟环境、安装上游 requirements.txt 并执行 pip check；安装阶段不创建管理员。
3. 双击“启动工作台.cmd”，或执行下列命令。脚本复用已运行的本项目服务，启动业务与工具；业务尝试启动本机上游 API。

```powershell
Set-Location D:\DouK\DouK-Web
.\install.ps1 -PythonPath 'C:\Python312\python.exe'
.\start.ps1 -NoBrowser
```

PythonPath 请换成本机已安装路径；环境已安装时不用重复运行 install.ps1。若系统策略阻止脚本，交由部署人员按单位策略处理。

首次启动且数据库没有管理员、admin 用户名未被其他人员占用时，自动创建 admin / 123456。已有管理员时不覆盖账号、不重置密码；admin 被非管理员占用时明确失败，不覆盖人员数据。默认口令是用户指定的初始口令，不是随机口令，也没有强制首次修改流程；对外交接前应由维护人员安排更换。

管理员本机入口 http://127.0.0.1:8877/admin；员工入口 http://127.0.0.1:8877/report。配置采集连接后，使用获授权的真实作品做目标环境验收。

## 3. 局域网、重启与异地使用

在管理员 PowerShell 中执行，网卡名称换成实际使用的网卡：

```powershell
Set-Location D:\DouK\DouK-Web
.\setup-lan.ps1 -InterfaceAlias '以太网'
```

脚本保存网络设置，为指定运行程序及网卡开放本地子网 TCP 8877，并创建当前 Windows 用户登录时运行的计划任务。它不是电脑开机后无须 Windows 登录即可启动的系统服务。使用 -NoAutostart 可不创建该登录启动任务。

员工使用“系统设置 → 访问与备份”显示的服务器局域网地址。127.0.0.1 永远指访问者自己的电脑，不能发给另一台电脑。服务器需保持开机、网络连接和后台运行；老板浏览器关闭不影响后台，电脑关机或休眠会中断访问。

“打开员工报送.cmd”调用 start-report.ps1，检查业务和报送页健康后打开 /report。stop.ps1 根据本项目绝对路径匹配进程，不应手动杀死占用端口的未知程序。

另一家公司可独立部署一套网站，数据不会自动同步。本版本限制本地/私网客户端、实际本机 IPv4 或 localhost Host 和同源 Origin，不信任转发头；没有现成公网 HTTPS、域名或反向代理部署方案。跨网共用需另行设计部署，不能把端口转发当作完成交付。

## 4. 当前页面与代码入口

管理端共有数据总览、数据统计、作品管理、每日报送进度、报送核查、部门与人员、抖音账号管理、系统设置八个页面。设置只含报送规则、采集连接、操作日志、访问与备份四项。旧 #checks、#missing 链接转向每日进度。

- server.py：HTTP 路由、认证、静态白名单、上游调用及启动。
- operations.py：管理员、绑定、作品、统计和后台刷新；部分旧巡检代码仅作历史兼容。
- public_reports.py：免登录报送、自动建档、回执、审核、备注和实际删除。
- report_accounts.py、douyin_login.py：多账号核验凭据及专用 Chrome 登录会话。
- daily_report_targets.py、report_progress.py：统一/个人/单日目标、免报与每日进度。
- reports.html/js/css：员工页；index.html、ops-ui.js、ops-flow-ui.js、reporting-admin.js 等为管理员页面。
- lan_access.py、workspace_backups.py、log_store.py：网络约束、一致性备份、日志。
- tools_server.py、tools.html 等：独立采集工具。
- build_docs.py、build_release.py：生成离线 HTML 文档和干净交付包。

旧 work_targets.py、account_coverage.py、巡检历史表及兼容接口的存在，不表示产品仍支持巡检。保存 auto_inspect=true 被拒绝；旧排队巡检取消，不能新建或重试 inspect；正常作品详情刷新仍使用后台任务。

## 5. 数据与身份模型

业务库为 data/workspace.sqlite；独立工具库为 data/tools/workspace.sqlite。连接配置在各自的 settings.json，网络配置为 data/network.json。Downloads 为下载媒体；专用浏览器资料与本机备份不属于可公开文件。

组织由 ops_departments、ops_groups、ops_users 及人员关联表管理；ops_bindings 关联抖音账号与责任人；ops_works、ops_submissions、作品归属快照保存作品和有效报送；ops_report_batches/items/claims 保存报送批次、逐条处理和核验快照；ops_report_excuses 保存单日免报。每日规则按生效日留版本，任务和日志也存 SQLite。具体迁移以各模块的建表和迁移代码为准，不能手工清空旧表解决页面问题。

管理员密码为加盐 PBKDF2-SHA256；会话令牌散列入库，Cookie 为 HttpOnly、SameSite=Strict，有效期 12 小时。登录失败限制为 15 分钟内 5 次；密码长度校验 6–128 位。员工不需要网站密码，填写姓名不能严格证明真实填报人身份。

核验凭据为随机能力令牌，散列入库，有效期 7 天；一次报送最多接收 100 个核验令牌。服务器独立验证状态、有效期及 sec_uid，不信任客户端自行声明的作者。无任何有效核验时，公开报送接口在创建名册或批次前返回 400。内部兼容函数仍能处理历史无核验记录，不代表公开入口允许绕过。

任一有效核验账号与作品作者 sec_uid 匹配且绑定/人员状态允许时可自动通过；均不匹配进入待核实。管理员可以结合员工备注处理。停用不可用删除或换令牌绕过；核验过期不会倒改历史已保存的核验快照。证明发布账号一致不等于证明内容原创。

## 6. 每日完成数与日期语义

目标优先级：当日免报、个人当日要求、个人常规要求、统一要求。规则按生效日和版本选择；数量为 0 表示免报，继承与 0 不同，允许范围 0–1000。个人常规数量覆盖统一数量，执行日和截止时间仍按适用规则；单日安排可覆盖休息日。不要在代码或文档写死现场每天必须几条。

有效完成按作品的北京时间发布日期、已确认责任人和作品 ID 去重。作品必须已识别、有效归属审核通过且未删除；发布时间晚于查询时刻的不计入。报送时间用于判断是否迟交，截止 18:00 包含 18:00:59。重复报送不重复计数，补交昨天发布的作品属于昨天。

待核实不计有效完成；同一作品待处理去重。发布时间未知时待处理可暂按接收日归类。早于首个新规则生效日的旧日期使用兼容报送计数，quota_mode=false，不能与目标模式混读。

进度接口默认 status=incomplete，聚合 missing（不足且无待处理）和 attention（不足且有待处理）；保留原子筛选兼容旧调用。counts.incomplete 等于两者之和；counts.attention 是其中子集。名单和 Excel 导出共用过滤逻辑。rejected 不计 pending_count/unlocated_count，也不会使历史兼容模式误判为已报送。

报送列表与公开回执返回 approval_method：automatic、manual、recorded 或空串。它与 account_match 分开：人工接受代报不会把 mismatch 改成 match。来源取已保存的 reviewer_id 和核验快照，历史无依据时保留 recorded。重试后自动归档会清理旧的重试审核人标记，避免误显示人工通过；操作日志保留。公开回执不返回 reviewer_id 或登录身份快照。

员工控制器对 queued/processing 使用约 4 秒轮询，对 review/failed 使用约 30 秒轮询；隐藏页面暂停，恢复焦点或可见时重新拉取（1.5 秒去重），终态不持续轮询。刷新保留备注草稿、文本选区与正在保存状态，显示最近成功读取时间。平台请求行为未因此增加。

人员范围是当前启用且在查询日结束前建档的名册；历史查询使用当前组织和启用状态，并非完整的人事历史快照。只有先把应报人员加入名册，才能发现从未报送的人。

作品列表按发布日期筛选；报送核查按接收时间筛选。目标模式的“查看作品”查看责任人当天发布作品，“查看报送”查看填报人记录，不强行把报送日期限制成发布日。导出包含全部筛选结果，不限当前页。

## 7. 报送接口与管理接口

除下载和导出外，接口使用 JSON。以下是当前常用入口摘要，字段校验以路由实现为准。

- GET /api/reporting/directory：可选组织人员。
- POST /api/reporting/login/start、status、cancel：开始、查询和取消账号核验。
- POST /api/reporting/submit：提交 reporter、links、request_id 和 login_tokens，兼容旧 login_token；至少一项有效核验是必需条件。最多 20 条作品链接，成功接收返回 202 和私有 receipt_token，不代表每条已识别成功。
- POST /api/reporting/receipt：使用请求体内的私有回执令牌读取该批次；POST /api/reporting/note：修改该回执条目的备注，最多 1000 字。
- GET /api/auth/status、POST /api/auth/login、logout：管理员认证。
- GET /api/ops/people、bindings、works、summary、reporting、settings：管理查询，均需管理员。
- GET /api/ops/report-progress 和 report-progress/export：按日期、组织、员工、状态查询及导出每日进度；列表每页 30 人。
- POST /api/ops/report-progress/excuse：单日免报/取消及原因。
- POST /api/ops/reporting/review：人工审核；POST /api/ops/reporting/delete：确认后批量实际删除。
- POST /api/ops/connection、connection/test：保存连接、测试本机 API。
- POST /api/ops/backups、GET /api/ops/backups/download：创建及下载业务快照；GET /api/ops/logs/export：导出日志。

request_id 用于幂等，不能在改变内容后复用同一个请求 ID。登录、核验和回执令牌放请求体或受控 Cookie，不放 URL。匿名用户不能按员工 ID 获取全体回执。当前浏览器显示最近 5 个本机回执；同网其他设备不自动共享，复制或共享浏览器资料可能共享其凭据。

报送审核页面每页 30 条，表内滚动、固定表头、两行文案和详情弹窗；全选只选当前页可删除条目。账号匹配结果和人工审核状态是两个概念，人工通过不会伪造“作者匹配”。

## 8. 删除、停用与后台刷新

员工、绑定、作品和报送的删除为实际业务删除；日志保留，不再把删除转换成停用。删除一件作品会清理同作品关联报送、统计、有效提交等，之后可以重新报送；删除员工/绑定会涉及其关联记录，界面需先确认范围。删除不是可从网页撤销的操作。

服务端要求相应删除请求 confirmed:true、permanent:true，批量报送 ids 为 1–100 项；UI 仍限制当前页。处理中记录或冲突后台任务会阻止删除，异步写回再次校验记录状态。旧软删除兼容字段和恢复接口不代表新版可恢复，业务恢复需使用备份。

停用是 enabled 状态变化，保留记录并阻止相应后续使用。清除本机回执仅移除浏览器显示，不清理业务库。刷新作品数据仍调用详情接口；缺失指标展示暂无数据，有最近成功值则保留其值和更新时间，不将缺失伪装成 0。

## 9. 备份、升级和恢复

网页备份使用 SQLite backup API，完成后 quick_check 校验。后台启动及每 15 分钟检查是否需要当日备份；按北京时间每天有一份即可，默认保留最近 14 份。停机时不执行。它只备份业务库，不是整项目备份；含账号及部分凭据信息，应限制访问。

修改或升级前另建带时间戳的本地完整备份：代码、文档、配置、业务和工具库；数据库用一致性快照，记录文件清单与 SHA256。需要保留的媒体、浏览器资料也应单独纳入范围并说明。不能直接复制正在写入的 SQLite 主文件作为完整备份。

恢复步骤：

1. 通知使用者暂停报送，使用 stop.ps1 停止本项目相关服务。
2. 再备份当前文件、数据库及可能的 WAL/SHM，保留为独立版本；不得覆盖唯一原件。
3. 检查目标快照可读取且 quick_check=ok，按该备份清单恢复兼容的代码/配置或数据库。离线替换数据库时由维护人员处理旧 WAL/SHM，不能把它们与另一时间点快照混用。
4. 启动服务，检查登录、页面、关键记录、规则和备份；需要重报恢复点之后的业务时通知使用者。

只回退本次文档时仅恢复清单中的文档、帮助文案及生成脚本，不回退业务库，以免丢掉新报送。跨电脑迁移浏览器登录资料可能失效，需重新核验。

## 10. 验证、文档与打包

```powershell
Set-Location D:\DouK\DouK-Web
.\.venv-api\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
node --check reporting-admin.js
.\.venv-api\Scripts\python.exe build_docs.py
.\.venv-api\Scripts\python.exe build_release.py
```

Node 需在 PATH。测试使用临时 SQLite、模拟上游和隔离 HTTP 服务；不向正式库造员工或作品。tests/preview_operations.py 可在 localhost:8878 预览模拟业务，不能冒充真实扫码验收；不要与正式地址混用测试凭据。

docs 下 Markdown 是维护源；build_docs.py 生成四份可离线阅读的 HTML，网站 /help 通过白名单开放。改 MD 后必须重建并检查链接，同时更新帮助文案及验证记录。data、配置、源码不通过文档路径公开。

build_release.py 使用白名单收集网站源码、测试、文档和上游源码/许可，排除个人数据、Cookie、浏览器资料、虚拟环境、下载媒体及日志；生成 ZIP 和 SHA256 清单。发布前检查清单、压缩包完整性，并在干净目标机器验收。文档压缩包只含说明，不是完整可运行项目。

## 11. 分发许可与目标环境验收

只使用获授权账号和数据。分发 TikTokDownloader 及修改版须按 GPL-3.0 要求保留许可、版权和免责声明、说明修改并提供相应完整源码及构建安装资料；组合代码的适用范围按实际分发方案评估，不能理解为只放一份 license 即完成。参见项目 THIRD_PARTY_NOTICES.md 和上游许可。

离线测试不能证明手机扫码、真实抖音接口、每个视频权限、所有浏览器或局域网防火墙均可用。交接应在目标设备实际完成管理员登录、员工扫码、授权作品报送、审核、每日进度、导出、备份和另一台员工电脑访问。失败应保留真实错误，不能以模拟数据代替验收。
