// 安装/编辑预设共用的纯文案，不依赖聊天模块，避免循环导入时读取未初始化常量。
export const SKILL_SCRIPT_RULES =
  '【Skill 附带脚本规范（务必遵守）】\n'
  + '1. 脚本一律放在该 Skill 目录的 scripts/ 子目录下（如 <skill 根目录>/scripts/xxx.py），不要散落在根目录或其它子目录；'
  + '后续调用时 script 参数写相对 Skill 根目录的路径（如 scripts/xxx.py）。\n'
  + '2. 脚本读写文本/JSON 文件必须显式写 encoding="utf-8"（open(path, encoding="utf-8")、json.dump(..., ensure_ascii=False)）。'
  + '原因：本应用冻结版下 Python 的默认编码跟随系统 locale（简体中文 Windows 上是 GBK），'
  + '裸 open() 会把 UTF-8 文件读成乱码、把中文写成 GBK，导致中文路径/内容出错。\n'
  + '3. 不要在脚本里自行改写 stdout/stderr 编码（宿主已把 run_skill_script 子进程的 stdio 强制为 UTF-8）；'
  + '但脚本内部若再启动子进程、或写临时文件/日志，同样要显式指定 UTF-8。\n'
  + '4. 优先只用标准库（冻结版解释器里可用的第三方包以应用自带为准）；脚本应把关键产物路径打印到 stdout，方便用户核对。';
