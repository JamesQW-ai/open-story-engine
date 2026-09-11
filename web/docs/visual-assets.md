## 图片资源

所有图片均使用内置 `image_gen` 工具生成，已检查图像内容并复制至项目，运行时不依赖外部图片服务。它们用于氛围营造，不作为剧情或人物事实。

| 文件 | 用途 |
| --- | --- |
| `web/public/images/rainy-station.png` | 《雨夜候车室》首页、封面、开局与阅读手记 |
| `web/public/images/rainy-identities.png` | 《雨夜候车室》四张身份卡，共用四等分人物图集，按姓名绑定 |
| `web/public/images/reading-desk.png` | 导入页、空书架和其他故事的通用封面 |

## 最终生成提示词

### rainy-station.png

Use case: illustration-story. Asset type: cinematic hero background for a Chinese interactive novel reading website, landscape 1536x1024. Primary request: an atmospheric rain-soaked small Chinese railway station at night, empty waiting room lit by a few warm amber lamps, wet platform, old station clock and distant railway tracks fading into blue mist. Editorial literary cover illustration with realistic film-grain texture, refined painterly detail, moody ink navy and warm muted gold. Wide composition with the architectural focal point and glowing waiting room on the right, darker quiet rain and mist on the left where website text will be placed. Intriguing and inviting, no horror, no people, no typography, no logos, no watermark. This is atmosphere art, no plot spoilers.

### reading-desk.png

Use case: illustration-story. Asset type: atmospheric image for an interactive fiction website's import-book card and generic book cover. Create a landscape 1536x1024 refined cinematic painterly illustration: an open unmarked book on a dark wooden desk beside a rain-streaked window at night, warm brass reading lamp, a loose blank manuscript sheet, deep ink navy shadows and muted antique gold highlights, tactile paper grain, cozy literary atmosphere, minimal objects, no human figures, no readable writing or letters, no logos, no watermark. Book centered to right with quiet shadow on left for HTML overlay text. No fantasy objects or plot-specific clues.

### rainy-identities.png

Create a single wide 3:2 character selection portrait atlas for a Chinese literary mystery single-player game, FOUR EQUAL WIDTH vertical panels in ONE image, each portrait centered in its quarter, no gutters, no text, no letters, no watermark. Premium cinematic painterly realistic illustration, subtle paper grain, muted ink navy, warm amber rim lighting, rain night railway station bokeh backgrounds consistent with all four. Four distinct fictional Chinese adult characters from LEFT TO RIGHT: 1) Xu Chuan, young adult man with short dark hair in charcoal everyday jacket, holding phone low, alert concerned; 2) Tang Qi, adult woman investigative observer, shoulder-length black hair and dark coat, quiet resolute expression; 3) Chen Yan, male station supervisor about forty, neat dark navy uniform and crisp collar, reserved serious expression; 4) Jiang Xu, male night maintenance worker in his forties, weathered face, worn hooded raincoat, weary watchful expression. Each shown shoulders-up, face entirely in upper middle of its own quarter with ample dark space below at bottom 25% for UI overlay. No weapons, no other figures. This is one cohesive card-art sprite sheet, perfectly four equally spaced portraits side by side, composition suitable for CSS background cropping each quarter.

人物外观属于视觉设计；卡片身份介绍仍来自故事包。其他小说使用通用书桌图，不套用这四个人物的形象。

## 身份开场与长文插图（第四轮）

以下三张图片使用内置 image_gen 生成并复制到项目，源图保留。经视觉检查，候车厅时钟改为熄灭面板以免显示错误时间，信号室门改为关闭以匹配开场。它们只提供场景氛围，不表示人物已经完成某个行动。

| 文件 | 用途 |
| --- | --- |
| `web/public/images/rainy-waiting-hall.png` | 许川与姜序开场、候车厅长文段落 |
| `web/public/images/rainy-signal-room.png` | 唐栖开场、信号室场景 |
| `web/public/images/rainy-platform.png` | 陈砚开场、站台与末班列车段落 |

### rainy-waiting-hall.png

Use case: illustration-story. Create a standalone landscape cinematic painterly illustration for an interactive Chinese mystery novel, an empty small coastal railway station waiting hall at 23:10 during heavy rain, worn wooden benches, rain streaked glass doors, glowing red digital clock with indistinct digits, dim amber lamps, blue night outside. Refined ink navy and muted gold palette, subtle film grain, grounded realistic architecture, wide quiet composition. No people, no readable text, no logos, no watermark. Atmospheric reading divider, no plot-specific clues.

### rainy-signal-room.png

Use case: illustration-story. Create a standalone landscape cinematic painterly illustration for an interactive Chinese mystery novel, inside a small old railway signal equipment room on a coastal storm night, damp concrete walls, muted green metal door, faint indicator lamps, a narrow rain-darkened window, shallow reflections near the doorway. Grounded realistic environment, ink navy shadows, subdued amber highlights, subtle film grain, wide contemplative composition. No people, no readable text, no logos, no watermark, no dramatic destruction. Atmospheric reading divider.

### rainy-platform.png

Use case: illustration-story. Create a standalone landscape cinematic painterly illustration for an interactive Chinese mystery novel, a quiet small coastal railway station platform in heavy night rain, stationary last train with dimly lit empty windows, warm amber platform lamps receding into blue mist, shining wet ground, wide contemplative composition, refined ink navy and muted gold palette with subtle film grain. No people, no readable text, no logos, no watermark. Atmospheric reading divider.

候车厅修订：Edit only the red digital clock high on the wall: replace its display with a dark unlit blank black panel, showing absolutely no numerals, no time, no letters. Preserve all the benches, glass doors, rain, composition and lighting exactly. This image must not assert a specific time.

信号室修订：Edit only the doorway on the left: the green metal door must be fully shut and completely fill its frame. No exterior landscape, sea, railway light, or gap is visible through the doorway. Keep the room, equipment, right-hand window, rain, palette, framing and warm dim lighting otherwise the same. This is a closed signal room on a storm night. No people, no text.
