# codextocpa

用于本地 `CPA/CLIProxyAPI` 管理页的 Codex OAuth 辅助脚本集合。

当前仓库包含：

- `oauth_incognito_browser_login.py`
  通过 Playwright 打开本地管理页并执行 Codex OAuth 登录流程。
- `codex_fast.py`
  基于主脚本的快速版，输入与点击尽量瞬时完成。
- `oauth_login_helper.py`
  只负责本地 OAuth URL 申请、状态轮询和授权文件定位的辅助脚本。

## 说明

- 当前版本可用，但成功率偏低。
- 仓库不包含账号文件、授权结果、截图或浏览器缓存。
- 请仅在你自己拥有权限的环境和账号上使用。

## 依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

## 常用命令

```bash
python oauth_incognito_browser_login.py --management-key YOUR_KEY
python codex_fast.py --management-key YOUR_KEY
python oauth_login_helper.py --management-key YOUR_KEY
```
