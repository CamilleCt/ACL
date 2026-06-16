# 云端部署（GitHub Actions）— 电脑关机也能跑

本地版 `app.py`（带网页界面）只在你电脑开着时跑。下面这套让它在 GitHub 云端
**每 15 分钟**自动扫描、命中新房源发邮件给你，**完全免费、电脑关机照常**。

机密（Gmail 地址/密码/收件人）只存成 GitHub 加密 Secrets，**绝不进代码**。

---

## 一次性设置（约 10 分钟）

### 1. 注册 GitHub 账号
打开 https://github.com → Sign up（免费）。

### 2. 新建仓库
右上角 ➕ → New repository
- Repository name：随便起，比如 `rental-monitor`
- 选 **Public（公开）** ← 这样 Actions 免费额度无限。
  代码里没有任何密码（密码走 Secrets），公开也安全。
- 不勾任何初始化选项 → Create repository

### 3. 上传项目文件
在新仓库页点 **uploading an existing file**，把 `d:\Desktop\huarenjie` 文件夹里的文件拖进去。
**要上传**：
```
app.py  huarenjie_rent_scraper.py  xineurope_scraper.py  notifier.py
scan_ci.py  config.json  .gitignore  DEPLOY.md
.github/workflows/monitor.yml   ← 这个在 .github/workflows 子目录里
```
**不要上传**（含你的密码或无用）：`huarenjie_rentals.sqlite3`、`server.log`、`__pycache__`、`123.txt`、`*_test.*`、`*.csv/json` 旧导出。
（`.gitignore` 已帮你挡掉数据库，放心。）

> 提示：网页上传不方便建子目录时，可先上传其它文件，再单独点
> Add file → Create new file，文件名输入 `.github/workflows/monitor.yml`，
> 把内容粘进去保存。

### 4. 添加 3 个密钥
仓库页 → **Settings** → 左侧 **Secrets and variables** → **Actions** →
**New repository secret**，依次加 3 个：

| Name | Secret（值） |
|------|------|
| `SMTP_USER` | `chentongfrance@gmail.com` |
| `SMTP_APP_PASSWORD` | 你的 16 位 Gmail 应用专用密码 |
| `EMAIL_TO` | `chentongfrance@gmail.com` |

### 5. 启动它
仓库页 → **Actions** 标签 → 左侧选 **rental-monitor** →
右侧 **Run workflow** 点一下（手动跑第一次，建立基线，不发邮件）。
之后它就会**每 15 分钟自动跑**，命中新房源给你发邮件。

---

## 日常使用
- **改搜索条件**：编辑仓库里的 `config.json`（站点、`max_rent`、关键词等）→ Commit。下次扫描即生效。
- **看它有没有在跑**：Actions 标签里能看到每次运行记录（绿勾=成功）。
- **状态**：`state.json` 记录已通知过的房源，由机器人每轮自动更新（你不用管）。

## 说明 / 限制
- GitHub 定时任务在高峰期可能延迟几分钟，偶尔跳过一次——找房足够用。
- 机器人每轮提交 `state.json` 算作仓库活动，所以定时任务不会因「60 天无活动」被自动停用。
- 想换频率：改 `.github/workflows/monitor.yml` 里的 `cron`（`*/15`=15 分钟，`*/30`=30 分钟）。

本地网页版仍可随时用：`python app.py` → http://127.0.0.1:8910（用来浏览/调参更直观）。
