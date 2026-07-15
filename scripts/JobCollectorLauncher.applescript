on run
    set launcherScript to "__LAUNCHER_SCRIPT__"
    set logDirectory to (POSIX path of (path to home folder)) & "Desktop/AI Job Intelligence Collector/logs"
    set launcherLog to logDirectory & "/launcher.log"
    set shellCommand to "/bin/mkdir -p " & quoted form of logDirectory & " && /usr/bin/nohup /bin/zsh " & quoted form of launcherScript & " >> " & quoted form of launcherLog & " 2>&1 </dev/null &"
    do shell script shellCommand
end run
