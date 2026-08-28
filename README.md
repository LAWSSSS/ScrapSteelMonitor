# ScrapSteelMonitor

盛隆废钢检判本地审核看板。扫一天的磅单，按人工结果对照 AI 料型占比和扣重，先审再写库。

默认**不会自动改库**。页面上点「批准」才 UPDATE/INSERT。

## 它做什么

- **监控当天**：满 2 条人工就判；只看重废 1/2 主料型。
- **审核历史日**：有 3 条人工就按 3 条平均；只有 2 条仍按 2 条。
- 料型差进 ±10% 带、扣重核算差进 150kg 内，就跳过。
- 写库前打快照，可以回滚。禁止 DELETE/DROP。

## 不要提交的东西

`monitor/.env` 里有数据库密码，已被 `.gitignore` 忽略。本机的 `state` / `pending` / `backups` / `logs` 也不进仓库。

新电脑复制 `.env.example` 为 `.env`，自己填密码。

## Mac 怎么跑

在项目根目录（能看到 `monitor` 文件夹）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r monitor/requirements.txt
cp monitor/.env.example monitor/.env
# 编辑 monitor/.env，填写 MYSQL_PASSWORD
```

连上公司 VPN 后：

```bash
python3 -m monitor dashboard
```

浏览器打开 http://127.0.0.1:8765

## Windows 怎么跑

看 [monitor/windows/运行说明.md](monitor/windows/运行说明.md)。

简要：装 Python 3.11/3.12（勾选 PATH）→ 双击 `monitor/windows/安装依赖.bat` → 填 `.env` 密码 → 连 VPN → 双击 `monitor/windows/启动看板.bat`。

两个 `.bat` 是 GBK 编码。不要另存成 UTF-8，否则窗口乱码。

## 连库

默认连内网：

```
MYSQL_HOST=172.16.16.101
MYSQL_PORT=33306
MYSQL_DB=slzj
```

VPN 不通会出现 MySQL 2013。看板刷新不连库；只有扫描才连库。

## 自检

不连数据库：

```bash
python3 -m unittest monitor.tests.test_config monitor.tests.test_scan monitor.tests.test_deduct
```

扫一天能列出车次，且不立刻 2013，就算通了。
