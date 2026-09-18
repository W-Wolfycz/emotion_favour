# 出图字体

`custom_t2i.html` 通过 `@font-face` 引用本目录字体（模板占位符 `{{ font_base }}`
在渲染时注入为 `file://` 绝对路径），所以字体随插件分发，不依赖用户系统字体，
跨设备显示一致。

| 文件 | 字体 | 模板中的 `@font-face` | 用途 | 来源 |
| --- | --- | --- | --- | --- |
| `ZCOOLXiaoWei-Regular.ttf` | 站酷小薇 | `EF Hero` | 页面标题、关系名、分组名 | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/zcoolxiaowei) |
| `ZCOOLKuaiLe-Regular.ttf` | 站酷快乐体 | `EF Title` | 正文、主调名 | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/zcoolkuaile) |
| `MaShanZheng-Regular.ttf` | 马善政毛笔楷书 | `EF Body` | 描述、提示、小标签 | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/mashanzheng) |

三款均为 SIL Open Font License 1.1，允许自由使用、修改与再分发（含随软件分发）；
许可正文见同目录 `OFL-*.txt`，每个字体一份并带各自版权行。

维护提示：

- 中文手写体一律不加 `font-weight`：字体本身字重已经足够，叠加会触发浏览器合成加粗、
  把笔画糊在一起；层级改用底色宽度、字号与字色表达。
- 新增或替换字体时：把 ttf 放入本目录 → 在 `custom_t2i.html` 的 `@font-face` 补一行 →
  同步本文件与 README 的「出图样式」小节。
- 模板不再引用的字体应及时删掉，避免无谓地增大插件包体积。
