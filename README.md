# LeafBridge

连接本地 Git 仓库与 Overleaf 的轻量命令行工具。目前已实现浏览器登录和本地会话保存；文件同步尚未实现。

## 使用 Conda 安装

在项目根目录执行：

~~~powershell
conda create -n leafbridge python=3.11 pip -y
conda activate leafbridge
python -m pip install -e .
leaf login
~~~

环境由 Conda 管理，放在标准环境目录，不放进项目文件夹。`pip install -e .` 使用可编辑安装，开发期间修改源码即可生效，不需要等项目完成。

已有项目环境时，安装或更新本地代码：

~~~powershell
conda activate leafbridge
python -m pip install -e .
~~~

`leaf login` 默认使用本机 Chrome，每次打开一个独立的新窗口。请先安装 Chrome；也可用 `leaf login --browser edge` 指定 Edge，或用 `--browser auto` 按 Chrome、Edge、Chromium 顺序尝试。

需要使用 Chromium 时，执行：

~~~powershell
python -m playwright install chromium
leaf login --browser chromium
~~~

也可以明确指定浏览器和登录等待时间：

~~~powershell
leaf login --browser chrome --timeout 600
~~~

登录窗口采用临时独立配置，不读取日常浏览器配置。请在 Overleaf 网页自行完成密码、验证码或单点登录。工具不会绕过验证；若网站拒绝自动化浏览器，登录会失败，需要进一步适配。关闭窗口或按 Ctrl+C 可取消。失败时保留原会话。

登录成功依据为 Overleaf 项目页面、CSRF Token 与有效会话 Cookie。工具保存该站点作用域内的 Cookie 和 CSRF Token，不保存密码，也不依赖项目列表页面的数据标签。

## 会话位置

- Windows：用户的 AppData/Roaming/leafbridge/session.json（遵循 APPDATA）。
- macOS：用户的 Library/Application Support/leafbridge/session.json。
- Linux：用户的 .config/leafbridge/session.json（遵循 XDG_CONFIG_HOME）。

会话以 JSON 原子写入。POSIX 系统的新文件权限为 0600，Windows 使用用户配置目录的访问权限。会话是登录凭据，不应分享或提交到 Git。Cookie 到期时需要重新登录；服务器也可能在到期前撤销会话。

## 离线测试

~~~powershell
conda activate leafbridge
python -m unittest discover -s tests -v
~~~

测试模拟浏览器，不使用真实账户，不连接 Overleaf。实际验证码和单点登录的兼容性需要在本机交互验证。

浏览器启动与 Cookie 接口依据 [Playwright 官方文档](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) 和 [BrowserContext 文档](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-cookies)。
