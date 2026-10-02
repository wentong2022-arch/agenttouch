// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
int main(void) {
    const char *home = getenv("HOME");
    if (!home) return 1;
    char script[1024];
    snprintf(script, sizeof script, "%s/.agentpet/agentpet_host.py", home);
    pid_t pid = fork();
    if (pid == 0) {
        execl("/usr/bin/python3", "python3", script, (char*)NULL);
        _exit(127);
    }
    int st = 0;
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
