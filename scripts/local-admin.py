"""Local admin（本机资源管理）：执行唯一用户数据备份。"""
from pathlib import Path
import argparse
import sys

for parent in Path(__file__).resolve().parents:
    if (parent / "voice_terminal/local_layout.py").is_file():
        sys.path.insert(0, str(parent))
        break
from voice_terminal.local_layout import local_root
from voice_terminal.local_files import backup_user_data

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "recover", "clean-bytecode", "reclaim-candidates", "reclaim-update-tools"], help="backup（唯一备份）、recover（恢复更新）、clean-bytecode（回收字节码）、reclaim-candidates（回收已安装候选）、reclaim-update-tools（回收临时更新工具）")
    args = parser.parse_args()
    if args.action == 'backup':
        from voice_terminal.distribution import require_stopped
        from voice_terminal.local_update import update_lock
        root = local_root()
        with update_lock(root):
            require_stopped(parent, root)
            result = backup_user_data(root)
        print(f"用户数据备份已校验：{len(result['files'])} 个文件；仅保留一份滚动备份。")
    elif args.action == 'clean-bytecode':
        from voice_terminal.local_files import clean_component_bytecode
        print('已回收清单外字节码：' + str(clean_component_bytecode(local_root())))
    elif args.action in {'reclaim-candidates', 'reclaim-update-tools'}:
        from voice_terminal.local_files import remove_tree
        root = local_root()
        if (root / 'tmp/update-transaction.json').exists():
            raise RuntimeError('未完成事务所需的临时资源不能回收')
        names = ('python-candidate', 'dsh-candidate', 'models-candidate') if args.action == 'reclaim-candidates' else ('update-tools',)
        for name in names:
            remove_tree(root / 'tmp' / name, root)
    else:
        from voice_terminal.local_update import recover, update_lock
        from voice_terminal.distribution import require_stopped
        root = local_root()
        with update_lock(root):
            if (root / 'tmp/update-transaction.json').exists():
                require_stopped(parent, root)
            print('更新恢复状态：' + recover(root))
