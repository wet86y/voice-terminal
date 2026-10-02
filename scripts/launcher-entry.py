"""Frozen entry（打包入口）：只引入轻量应用宿主。"""
from voice_terminal.application.launch import main

if __name__ == "__main__":
    main()
