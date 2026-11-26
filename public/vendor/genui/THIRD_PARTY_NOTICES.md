# Third-party notices

## dsh-genui and Cat Chat's browser distribution

Cat Chat embeds the dsh-genui browser renderer under the MIT license in
[LICENSE](LICENSE). The corresponding upstream source is
[omdsh-dev/dsh-genui v0.17.0](https://github.com/omdsh-dev/dsh-genui/tree/a5c4232c9ea0caac118c388ed1b519e407bbd71b).
Cat Chat's integration and the adapted GenUI Skill are part of this repository.

The seven `assets/` files match the contents of `package/lib/assets/` in the
[official npm release archive](https://registry.npmjs.org/@changfenhuang/dsh-genui/-/dsh-genui-0.17.0.tgz)
byte for byte. The npm integrity value is
`sha512-NNQLGhGurdXEIS187IES7XKqt/XiyCeO+Or/9WGYGEyG+MH+oip4K06EbECHIDafIWzV00fmsPVl10tS877M5g==`.

| Local file | SHA-256 |
| --- | --- |
| `assets/echarts-core.js` | `5eed01ed23a0246559d14f59c2ff5fc1b4fcca13558c2ed763a73724cd2b01e0` |
| `assets/echarts-full.js` | `53914c2d4957bf29614738f34ce14698ae6d677e5d595a88b79fadef2881c644` |
| `assets/icons.js` | `1b2bb5c7256fb28de549cf0f43fa908dc8ae674ed4d6d13006909f9995aa1fe1` |
| `assets/maplibre.js` | `895cb7463511a787dd6e742195744011e54b676253540fbe572a67cfc6350180` |
| `assets/mermaid.js` | `95ca96372cd16e2354a311c70fbf67d7f28a134394e0fa9de0b7347d8677a0f5` |
| `assets/standalone-runtime.js` | `59f8f45789d53132fd3f210027decdc89b129290eb3866ad18274be2f1d00f17` |
| `assets/three.js` | `3b61ad28030d938519dd52db05152690ef01c58f17bfdb5cd8c4579b25e1c7a9` |
| `cat-genui.js` (Cat Chat-specific build) | `c8b18d1b4f584e65636b68b0e9ae3683caef9fdb74c3729613a66a99372a3e4c` |

`cat-genui.js` exposes `window.__CatGenui__` for Cat Chat and reports dsh-genui
0.17.0. Its embedded tabbable header identifies 6.5.0. The verified npm
standalone runtime contains tabbable 6.5.1. The custom entry point and its build
lock file are unavailable here. The checksum identifies the local bytes and
does not establish a reproducible build or a complete inventory of that custom
file.

## OpenAI Apps SDK UI icons (MIT)

The icon glyphs bundled in `src/client/icons/icon-data.ts` are copied from the
OpenAI Apps SDK UI icon set:

- Source: <https://github.com/openai/apps-sdk-ui>
- Pinned commit: `0f00143c7a639906f1621fe58e1b6be7b5bea46d`
- Upstream path: `src/components/Icon/svg`
- Vendored as: literal SVG geometry and attributes (no upstream component code), curated to 749 glyphs (64 inlined, 685 lazy-loaded); six glyphs with unsupported nested structures are excluded in full and listed in `ICON-NAMES.md`

```
Copyright 2025 OpenAI

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The colour values referenced by the `--dsl-g-*` token layer are *not* copied
from that project: they resolve through the host's `--dsw-alias-*` tokens, and
the literals in `GenuiBlock.module.css` are last-resort fallbacks.

## Engines and embedded dependencies

The browser loads these engines from the distributed local files, on demand.
The engine code and its license obligations are part of the Cat Chat
distribution even when a conversation does not load the asset.

| Files | Main components |
| --- | --- |
| `cat-genui.js`, `assets/standalone-runtime.js` | React and React DOM 18.3.1, Scheduler, KaTeX 0.16.47, Floating UI and tabbable, under their respective MIT licenses |
| `assets/echarts-core.js`, `assets/echarts-full.js` | Apache ECharts 5.6.0 (Apache-2.0), ZRender 5.6.1 (BSD-3-Clause) and tslib 2.3.0 (0BSD). The full variant also includes echarts-wordcloud 2.1.0 (ISC), incorporating wordcloud2.js (MIT) |
| `assets/mermaid.js` | Mermaid 11.16.0 (MIT), with DOMPurify 3.4.13 (Apache-2.0 or MPL-2.0), KaTeX 0.16.47, js-yaml 4.1.1, Lodash ES 4.18.1 and other dependencies |
| `assets/three.js` | Three.js 0.180.0 (MIT) |
| `assets/maplibre.js` | MapLibre GL JS 6.16.0 (BSD-3-Clause), including its worker and CSS |

See [DEPENDENCY-LICENSES.md](DEPENDENCY-LICENSES.md) for the license texts and
Apache ECharts NOTICE. The dependency list also retains notices for candidates
in the pinned upstream browser dependency graph that may have been eliminated
by bundling. Existing license comments in the scripts are retained.

## MapLibre GL JS 6.16.0 (BSD-3-Clause)

The lazy maplibre asset bundles the pinned official npm distribution, including
its ESM main, worker, and CSS. Source: https://github.com/maplibre/maplibre-gl-js/tree/v6.16.0

Upstream dsh-genui packaging modifications: the official worker is rebundled as a
classic-compatible IIFE. A version/digest-checked worker-factory adaptation
uses a native classic Worker only for this engine's exact owned Blob on
`file://`. See the pinned upstream
[`tsdown.config.ts`](https://github.com/omdsh-dev/dsh-genui/blob/a5c4232c9ea0caac118c388ed1b519e407bbd71b/tsdown.config.ts).
The upstream license text follows unchanged.

```text
Copyright (c) 2023, MapLibre contributors

All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of MapLibre GL JS nor the names of its contributors
      may be used to endorse or promote products derived from this software
      without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


-------------------------------------------------------------------------------

Contains code from mapbox-gl-js v1.13 and earlier

Version v1.13 of mapbox-gl-js and earlier are licensed under a BSD-3-Clause license

Copyright (c) 2020, Mapbox
Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name of Mapbox GL JS nor the names of its contributors
  may be used to endorse or promote products derived from this software
  without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


-------------------------------------------------------------------------------

Contains code from glfx.js

Copyright (C) 2011 by Evan Wallace

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

--------------------------------------------------------------------------------

Contains a portion of d3-color https://github.com/d3/d3-color

Copyright 2010-2016 Mike Bostock
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the author nor the names of contributors may be used to
  endorse or promote products derived from this software without specific prior
  written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

```
