# 英飞凌 Keysight ADS 器件库下载说明

> 确认时间：2026-10-30。目标：英飞凌官方给 Keysight ADS 的器件仿真库（用户确认选择“英飞凌官方 ADS 器件库”）。
> 说明：英飞凌没有叫 “LCR” 的库——它不做无源电感/电容/电阻；官方 ADS 库是器件库（LNA、RF 晶体管、TVS/RF 二极管、MMIC）。

## 1. 目标库

- 名称/版本：`Infineon-RFTransistor-Keysight_ADS_Design_Kit-SM-v02_10-EN`
- 下载页（需登录）：
  https://www.infineon.com/gated/infineon-rftransistor-keysight-ads-design-kit-simulationmodels-en_b35abef5-6c01-4845-b1ab-94a406a7a854
- zip 直链（同样需要登录后才能下载）：
  https://www.infineon.com/content/dam/infineon/row/public/documents/30/50/infineon-rftransistor-keysight-ads-design-kit-simulationmodels-en.zip

## 2. 为什么脚本/命令行无法直接下载（已实测）

- 匿名访问 zip 直链 → 301 重定向 → 返回 **myInfineon Login** 页面（HTML，非 zip）。
- 英文站、中文站 `infineon.cn`、`/assets/` 公共路径均被登录墙挡住。
- Wayback Machine（web.archive.org）当日暂时离线，无法取历史公开版本。
- 结论：**必须用免费注册的 myInfineon 账号登录后手动下载**（邮箱注册即可，无需企业账号）。

## 3. 下载步骤

1. 打开 https://www.infineon.com/myinfineon 免费注册并登录（或直接打开上面的下载页，点登录）。
2. 回到第 1 节的下载页，点击 Download 保存 zip。
3. 不要手动解压 —— ADS 安装时会自己解压（见下）。

## 4. 安装到 ADS（通用流程，ADS 2016+ 均适用）

1. ADS 主界面菜单：**DesignKits → Unzip Design Kit...** → 选择下载的 zip → 解压到本地目录（建议在 ADS 安装目录下单独建一个 `DesignKits/Infineon` 文件夹）。
2. **DesignKits → Manage Design Kits**（旧版为 Manage Libraries → Add Design Kit from Favorites...）→ 勾选刚解压的 Infineon 设计套件。
3. 打开原理图，在组件面板（Palette）下拉里会出现 Infineon 库（例如 `Infineon_WLAN` 中的 BGA622 等），直接拖入原理图。
4. 模型本质是封装好的 S 参数（库内含 `.s2p` 文件，如 `BGA622L7_2p75V_OnMode.s2p`），直流/偏置无需单独设置。

## 5. 备用入口

- 老入口 `http://www.infineon.com/32lib`：现在 301 跳转到 LNA 产品页
  https://www.infineon.com/design-resources/finder-selection-tools/simulation-model 的相关产品分类。
- 只需要单个器件模型（免登录）：infineon.com 搜型号 → 产品页 → Documents → **Simulation Models** 下载 `.s2p`/SPICE。
- 中文教程（库下载+安装+LNA 仿真全流程）：
  https://blog.csdn.net/sunskyday/article/details/106629252

## 6. 本地已下载的参考文件（工作目录 `downloads/` 下）

- `Infineon-Library-Installation-Guide-AWR-MWO-SM-v01_00-EN.pdf` —— 英飞凌组件库 **AWR Microwave Office** 版安装指南（9 页）。若你用的是 AWR 而非 ADS，看这份；其中说明组件库内容为 SPICE 模型 + MDIF S 参数 + 封装 footprint，老版本地库入口为 `infineon.com/32lib`。
- `gated_ads_kit.html` / `gated_ads_kit_cn.html` —— 英/中官网 gated 页面存档（用于确认直链）。

## 7. 如果你真正想要的是无源 LCR（电感/电容/电阻）库

那不是英飞凌的（英飞凌不产无源件），可选：
- 村田 Murata **SimSurfing**（提供 ADS 库下载）
- TDK、太阳诱电 Taiyo Yuden（“Component Library for Keysight ADS”）
- 国巨 Yageo / 三星电机 SEMCO
