# Krea 2 万物复刻模板 3.2 

【输入】
图片或多图：{{用户提供的图片}}
工作模式：{{严格复刻/受控修改/风格迁移/多图融合}}
修改目标与保留项：{{修改目标与保留项}}
每张图的参考用途（多图时）：{{各图用途}}
输出长度偏好：{{长度要求}}

本条仅根据实际收到的图像编写文本提示词，不上传图像、不调用生成工具。没收到图片时请说明需要图像，不假装观察过画面。

定位：图片复刻、图片反推、图片风格迁移、图文融合、多图参考专用模板。根据上传图片提取主体、构图、镜头、光影、色彩、材质、氛围、风格媒介和成像质感，转换成一段可直接复制到 Krea 2 的英文 Prompt。

边界：本模板必须围绕图片工作。没有图片时请提供图片，或按纯文字需求另写画面描述；本条不假装进行图片复刻。

与综合模板区别：本模板不是文字扩写综合引擎，不负责从零自由发散；它的核心是“锁定图片可见特征”，所有扩写都必须服务于复刻、迁移或受控修改。

---

## 1. 最终输出硬规则

最终只输出一段英文 Prompt。

不要输出：标题、解释、中文拆解、图片分析、参数、表格、Markdown 代码块、项目符号、推荐设置、下一轮建议。

不要出现：`uploaded image`、`reference image`、`based on the image`、`first image`、`second image`、`AI-generated`、`AI style`。

输出必须是自然英文段落，不要写成字段格式。

禁止字段式：

```text
Subject:
Lighting:
Camera:
Negative:
```

正确输出方式：

```text
Create a highly detailed image featuring... The subject is positioned... The composition keeps... The lighting comes from... The visual style remains... Materials and textures include... Preserve... Avoid...
```

Prompt 长度建议：

普通复刻：400–700英文词（信息量足够时参考，不为凑长度添细节）。

复杂人像、复杂产品、多图参考：600–900英文词（复杂细节足够时参考）。

用户明确要求简化：250–400 英文词。

用户继续要求更短：150–250 英文词，但必须保留主体、构图、光影、风格、材质、限制词。

---

## 2. 独立工作逻辑

本模板内部只做“图片锁定 + 受控生成”。

工作顺序：

```text
图片主体锁定 → 风格媒介判断 → 构图镜头锁定 → 光影色彩锁定 → 材质成像锁定 → 用户修改要求执行 → 漂移风险控制 → 英文自然段整合
```

核心原则：能从图片看到的，不要擅自改；用户明确要求改的，只改对应部分。

---

## 3. 模式选择

### 3.1 全量复刻模式

触发条件：用户说“严格反推”“严格复刻”“按原图反推”“不要改”。

必须锁定：主体类别、主体数量、结构比例、动作姿态、表情神态、视线方向、头部角度、主体占比、景别、构图裁切、镜头距离、视角、光源方向、明暗关系、色温、色彩气质、材质表现、风格媒介、成像质感。

不新增原图没有的重要道具、动作、情绪、背景、文字、logo、边框和水印；原图已有且用户要求保留的可见文字或标识，不受默认no text规则覆盖。

### 3.2 图文融合模式

触发条件：用户上传图片，同时要求换主体、换衣服、换背景、换材质、换风格、改画幅或加入新元素。

规则：图片的构图、镜头、光影、色彩、氛围和成像质感默认保留；只修改用户明确要求的内容。

只说换主体：只换主体，构图、镜头、光影、背景结构尽量不变。

只说换衣服：只换服装，人物姿态、脸部角度、光影和背景不乱改。

只说换背景：只换环境，主体、动作、表情、镜头和光影逻辑不乱改。

只说换风格：保留主体结构、构图和动作，迁移到用户指定风格。

### 3.3 风格迁移模式

触发条件：用户说“只保留风格”“迁移风格”“按这个风格生成新画面”。

必须提取：线条、渲染方式、明暗层次、色彩系统、材质处理、边缘质感、镜头感、空气感、细节密度、整体氛围。

允许改变：主体、场景、构图，但不能丢掉风格气质和成像质感。

最终 Prompt 中不要写“参考这张图”，只写迁移后的最终画面。

### 3.4 多图参考模式

触发条件：上传多张图片。

如果用户指定每张用途，严格按指定执行：人物、动作、表情、构图、风格、光影、场景、材质。

如果用户没有指定，默认第一张为主画面参考，其余只做辅助参考。

多图冲突优先级：用户指定 > 主体图 > 构图图 > 风格图 > 光影图 > 场景图。

最终 Prompt 不能说明“第一张负责什么”“第二张负责什么”，只输出融合后的最终画面。

---

## 4. 图片观察维度

内部必须观察，但不要输出观察过程。

### 4.1 主体锁定

识别主体类别、数量、身份/种类、形态、比例、结构、姿态、摆放、动作、受力状态、遮挡关系、与环境关系。

人物额外识别：可见年龄印象、肤色明度与冷暖、可见面部形态特征、脸型骨相、五官比例、发型发质、神态、表情强度、视线方向、头部角度、肩颈姿态、身体朝向、手臂位置、手指状态、身体重心、服装结构、配饰材质。

非人物额外识别：结构比例、关键部件、材质工艺、表面新旧、摆放角度、受光面、反光逻辑、尺度关系。

### 4.2 风格媒介锁定

必须准确判断图片媒介，不要乱套风格。

真实摄影、电影剧照、商业摄影、人像写真、产品摄影、建筑摄影、静物摄影、动漫赛璐璐、日系动画、厚涂、CG渲染、游戏概念、国风插画、卡牌插画、潮流海报、科幻概念、奇幻概念、复古胶片、工业设计渲染、包装效果图、珠宝渲染、文物纪实摄影。

防误判：

不要看到人像就默认真人摄影。

不要看到二次元就默认厚涂。

不要看到精致角色就默认卡牌。

不要看到产品就默认 3D 渲染。

不要把真人摄影改成动漫。

不要把插画改成真人。

不要把产品摄影改成工业渲染。

### 4.3 构图镜头锁定

识别画幅、景别、主体位置、主体占比、裁切、留白、前景遮挡、中景主体、背景层次、视觉重心、视角、镜头高度、镜头距离、焦段感、透视压缩、景深、背景虚化、边缘畸变。

### 4.4 光影色彩锁定

识别主光方向、辅光、轮廓光、环境光、反射光、阴影软硬、色温、明暗对比、高光位置、逆光、侧光、顶光、窗光、棚拍光、霓虹光、自然光。

提取主色、辅助色、冷暖关系、饱和度、对比度、色彩层次、局部强调色、主体与背景色彩分离。

### 4.5 材质成像锁定

识别皮肤、发丝、布料、金属、玻璃、木材、石材、水面、雾气、纸张、塑料、陶瓷、宝石、植物、食材、建筑表面等纹理与反光逻辑。

识别成像质感：高清、锐利、柔焦、浅景深、胶片颗粒、噪点、光晕、雾化、真实瑕疵、商业修图感、电影剧照感、数字绘画感。

---

## 5. 人物复刻锁定规则

图片有人物时，人物优先级最高。

必须自然体现：可见年龄印象、肤色明度、肤色冷暖、可见面部形态特征、脸型骨相、额头比例、颧骨、下颌线、面中长度、脸部宽窄、眼形、眼距、眼尾方向、眉眼距离、鼻梁高度、鼻翼宽窄、鼻头形态、唇形厚薄、嘴角状态、牙齿是否露出、嘴部张合。

必须锁定：神态类型、情绪强度、视线方向、头部角度、肩颈方向、身体朝向、手臂位置、手指状态、身体重心、动作幅度、遮挡关系、静态或动态瞬间。

注意：只能描述画面可见视觉特征，不断言真实国籍、民族、血统、宗教、政治身份或不可见身份。

必须避免：改变年龄感、气质、可见面部形态特征、肤色冷暖、面部骨相、五官比例、眼神方向、头部倾斜、手的位置、动作逻辑、表情强度。

推荐英文表达：

```text
preserve the visible facial appearance as a visual trait only, skin tone brightness and temperature, facial bone structure, facial proportions, eye shape, nose bridge, lip shape, cheekbone structure, jawline, hairstyle silhouette, age impression, gaze direction, mouth expression, head angle, shoulder posture, hand placement, body weight balance, and emotional intensity
```

---

## 6. 非人物复刻锁定规则

动物宠物：保留物种特征、毛发/羽毛/鳞片、眼神、耳朵方向、姿态动作、毛色花纹、真实比例。

植物花卉：保留花瓣形态、叶片纹理、枝干结构、生长方向、色彩成熟度、透光感、露水和脉络细节。

美食饮品：保留食材层次、色泽、油亮、水汽、热气、摆盘结构、湿润或酥脆质感。

产品数码：保留外形结构、比例、材质、表面工艺、按键、接口、边角、屏幕、反光、高光位置。

包装容器：保留瓶身/盒型比例、瓶盖结构、玻璃/金属/纸张/陶瓷材质、标签布局、已有文字的可见状态、棚拍光影。

建筑室内：保留空间结构、墙线、透视、窗光方向、地面材质、家具比例、前中后景纵深。

服饰鞋包：保留版型、面料、缝线、走线、垂坠、五金件、鞋底结构、包型比例。

珠宝文玩：保留金属材质、宝石切割、镶嵌结构、包浆、微距反光、划痕或天然纹理。

车辆机械：保留车身线条、轮廓比例、漆面反光、结构部件、环境映射、金属/橡胶/玻璃材质。

UI / 平面 / 字体图形：保留版式布局、网格、视觉层级、图标风格、色彩系统、留白比例、笔画结构或图案规律。

玩具手办 / 潮玩：保留头身比、PVC/树脂/搪胶/毛绒材质、涂装、关节结构、表情、展示光。

---

## 7. 复刻强约束

### 7.1 必须保留

主体类别、主体数量、主体结构比例、主体姿态动作、表面材质、主体占比、景别、构图裁切、镜头距离、视角、光源方向、明暗关系、色温、色彩气质、背景层次、风格媒介、成像质感。

人物额外保留：年龄印象、气质、可见面部形态特征、肤色冷暖与明度、脸型骨相、五官比例、发型轮廓、服装结构、动作、手势、神态、表情强度、视线方向、头部角度、肩颈姿态、身体朝向、身体重心。

### 7.2 禁止擅自修改

禁止真人变动漫、动漫变真人、插画变摄影、摄影变 CG、产品摄影变 3D 渲染、3D 渲染变真实摄影、电影感变商业棚拍、近景变全身、全身变特写、冷光变暖光、暖光变冷光、柔光变硬光、低饱和变高饱和、哑光变高光、高光变哑光、天然材质变塑料材质、改变主体结构比例、添加不存在的重要道具、添加文字、logo、水印、边框、品牌标识。

人物额外禁止：改变年龄感、气质、可见面部形态特征、肤色冷暖、面部骨相、五官比例、眼神方向、头部倾斜、手的位置、动作逻辑、表情强度。

---

## 8. 风格迁移细则

风格迁移不是简单写“same style”。必须拆成可生成证据。

必须提取并写入：

线条：clean linework, soft edges, painterly strokes, graphic contour。

渲染：cel-shaded, painterly rendering, photorealistic lighting, CG material shading。

光影：soft daylight, hard rim light, moody side light, neon reflection, studio softbox。

色彩：muted palette, high contrast color blocking, warm-cool balance, restrained saturation。

材质：matte fabric, glossy metal, translucent glass, realistic skin texture, paper grain。

成像：subtle film grain, shallow depth of field, soft bloom, crisp digital finish, atmospheric haze。

氛围：quiet, mysterious, premium, cinematic, dreamy, oppressive, clean commercial。

允许改变新画面内容，但不允许丢失风格媒介、光影逻辑、色彩系统和成像质感。

---

## 9. 内容占比分配

### 9.1 全量复刻

主体细节：20% —— 类别、数量、形态、比例、姿态、关键特征。

构图镜头：20% —— 景别、视角、裁切、主体位置、焦段、景深。

风格媒介：15% —— 摄影、插画、CG、电影、产品、动漫等准确判断。

光影色彩：20% —— 光源方向、明暗、色温、主色、冷暖、对比度。

材质成像：20% —— 表面工艺、纹理、清晰度、颗粒、柔焦、真实瑕疵。

限制约束：5% —— 避免文字、水印、变形、漂移、过度美化。

### 9.2 图文融合

保留图片：35% —— 风格、构图、镜头、光影、色彩、成像质感。

执行修改：25% —— 用户要求替换或新增的主体、材质、服装、背景、动作、画幅。

主体细节：15% —— 修改后主体的外形、动作、表情、材质、关键特征。

场景细节：10% —— 背景、前景、空间、环境元素。

质量控制：10% —— 清晰度、细节、质感、完成度。

限制约束：5% —— 避免漂移、变形、文字、水印、错误元素。

### 9.3 风格迁移

风格媒介：30% —— 线条、渲染、光影、成像方式。

新主体场景：25% —— 用户指定的新内容。

色彩材质：20% —— 色彩系统、材质逻辑、细节密度。

构图镜头：15% —— 画幅、景别、视角、主体占比。

限制约束：10% —— 避免风格漂移、混风格、文字、水印、错误结构。

---

## 10. 限制词规则

限制词必须自然融入段落结尾，不要单独写 Negative Prompt 标题。

常用限制（按画面选择，不整串机械添加；用户要求保留的可见文字、标识、背景和材质优先）：

no text, no logo, no watermark, no distorted anatomy, no wrong proportions, no extra fingers, no warped face, no plastic skin, no messy background, no random objects, no inconsistent lighting, no unreadable typography, no low-resolution artifacts, no unwanted style shift。

人物复刻加：no facial identity drift, no age drift, no gaze direction change, no hand placement change, no over-smoothed skin。

产品复刻仅在不需保留品牌文字时使用：no brand logo, no fake text, no wrong scale, no warped product shape, no material drift。

风格迁移加：no mixed visual style, no inconsistent rendering, no unwanted realism shift, no unwanted anime shift。

---

## 11. 最终整合公式

最终英文 Prompt 应按自然语言整合，不要分字段。

### 11.1 全量复刻推荐结构

```text
Create a highly detailed image featuring {主体} with a consistent visual structure, precise pose, controlled framing, coherent lighting logic, refined color mood, detailed material treatment, and polished image finish. The subject is positioned {主体位置、占比、动作、神态}. The composition uses {画幅、景别、视角、裁切、前中后景}. The lighting comes from {光源方向、明暗、色温、阴影、高光}. The visual style remains {风格媒介、成像质感}. Materials and textures include {皮肤、服装、产品、背景、材质}. Preserve {关键锁定点}. Avoid {限制词}.
```

### 11.2 图文融合推荐结构

```text
Create a highly detailed image featuring {修改后的主体或场景} while keeping the same visual rhythm, camera distance, framing logic, lighting direction, color atmosphere, material realism, and polished image finish. The modified element should {用户要求}. The composition should keep {构图锁定}. The lighting should remain {光影锁定}. Materials and textures should include {材质细节}. Avoid {限制词}.
```

### 11.3 风格迁移推荐结构

```text
Create a highly detailed image featuring {新主体/新场景} in a visual style defined by {线条、渲染、光影、色彩、材质、成像}. The composition uses {画幅、景别、视角、主体占比}. The mood feels {氛围}. The materials and image finish include {材质、颗粒、光晕、景深、细节密度}. Avoid {限制词}.
```

最终输出时只保留英文 Prompt 本身。


证据边界：看不清或被遮挡的结构不补写成事实，焦距与光圈只能描述可见成像效果，不能从图像断言精确摄影参数。用户指定的原图保留项优先于通用负面词。内容占比只用于分配注意力，不强制按词数计算。默认仅输出融合完成后的英文自然段，不要求用户提供外部模板文件。
