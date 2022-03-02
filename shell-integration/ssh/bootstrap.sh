#!/bin/sh
# Copyright (C) 2022 Kovid Goyal <kovid at kovidgoyal.net>
# Distributed under terms of the GPLv3 license.

# read the transmitted data from STDIN
cleanup_on_bootstrap_exit() {
    [ ! -z "$saved_tty_settings" ] && command stty "$saved_tty_settings"
}

die() { echo "$*" >/dev/stderr; cleanup_on_bootstrap_exit; exit 1; }
debug() { printf "\033P@kitty-print|%s\033\\" "$(printf "%s" "debug: $1" | base64)"; }

saved_tty_settings=$(command stty -g)
command stty raw min 1 time 0 -echo || die "stty not available"
trap 'cleanup_on_bootstrap_exit' EXIT

data_started="n"
data_complete="n"
pending_data=""
if [ -z "$HOSTNAME" ]; then
    hostname=$(hostname)
    if [ -z "$hostname" ]; then hostname="_"; fi
else
    hostname="$HOSTNAME"
fi
# ensure $HOME is set
if [ -z "$HOME" ]; then HOME=~; fi
# ensure $USER is set
if [ -z "$USER" ]; then USER=$(whoami); fi

# ask for the SSH data
data_password="DATA_PASSWORD"
password_filename="PASSWORD_FILENAME"
pending_data=""
data_complete="n"

printf "\033P@kitty-ssh|%s:%s:%s\033\\" "$hostname" "$password_filename" "$data_password"
size=""

untar() {
    command base64 -d | command tar xjf - --no-same-owner -C "$HOME" 
}

get_data() {
    # We need a way to read a single byte at a time and to read a specified number of bytes in one invocation.
    # The options are head -c, read -N and dd
    #
    # read -N is not in POSIX and dash/posh dont implement it. Also bash seems to read beyond
    # the specified number of bytes into an internal buffer.
    #
    # head -c reads beyond the specified number of bytes into an internal buffer on macOS
    #
    # POSIX dd works for one byte at a time but for reading X bytes it needs the GNU iflag=count_bytes
    # extension. 
    while :; do
        n=$(command dd bs=1 count=1 2> /dev/null) 
        if [ "$n" = ":" ]; then break; fi
        size="$size$n"
    done
    # using dd with bs=1 is very slow on Linux, so use head 
    command head -c "$size" | untar
    # command dd bs=1 "count=$size" 2> /dev/null | untar
    rc="$?";
}

get_data
command stty "$saved_tty_settings"
saved_tty_settings=""
if [ "$rc" != "0" ]; then die "Failed to extract data transmitted by ssh kitten over the TTY device"; fi
if [ -n "$pending_data" ]; then
    printf "\033P@kitty-echo|%s\033\\" "$(printf "%s" "$pending_data" | base64)"
fi
if [ ! -f "$HOME/.terminfo/kitty.terminfo" ]; then die "Extracted data transmitted by ssh kitten is incomplete"; fi

# export TERMINFO
tname=".terminfo"
if [ -e "/usr/share/misc/terminfo.cdb" ]; then
    # NetBSD requires this see https://github.com/kovidgoyal/kitty/issues/4622
    tname=".terminfo.cdb"
fi
export TERMINFO="$HOME/$tname"

# compile terminfo for this system
if [ -x "$(command -v tic)" ]; then
    tic_out=$(command tic -x -o "$HOME/$tname" "$HOME/.terminfo/kitty.terminfo" 2>&1)
    rc=$?
    if [ "$rc" != "0" ]; then die "$tic_out"; fi
fi

# If a command was passed to SSH execute it here
EXEC_CMD

shell_integration_dir="$HOME/SHELL_INTEGRATION_DIR"

login_shell_is_ok() {
    if [ -z "$login_shell" -o ! -x "$login_shell" ]; then return 1; fi
    case "$login_shell" in
        *sh) return 0;
    esac
    return 1;
}

detect_python() {
    python=$(command -v python3)
    if [ -z "$python" ]; then python=$(command -v python2); fi
    if [ -z "$python" ]; then python=python; fi
    if [ -z "$python" -o ! -x "$python" ]; then return 1; fi
    return 0;
}

using_getent() {
    cmd=$(command -v getent)
    if [ -n "$cmd" ]; then 
        output=$($cmd passwd $USER 2>/dev/null)
        if [ $? = 0 ]; then 
            login_shell=$(echo $output | cut -d: -f7);
            if login_shell_is_ok; then return 0; fi
        fi
    fi
    return 1;
}

using_id() {
    cmd=$(command -v id)
    if [ -n "$cmd" ]; then 
        output=$($cmd -P $USER 2>/dev/null)
        if [ $? = 0 ]; then 
            login_shell=$(echo $output | cut -d: -f7);
            if login_shell_is_ok; then return 0; fi
        fi
    fi
    return 1;
}

using_passwd() {
    cmd=$(command -v grep)
    if [ -n "$cmd" ]; then 
        output=$($cmd "^$USER:" /etc/passwd 2>/dev/null)
        if [ $? = 0 ]; then 
            login_shell=$(echo $output | cut -d: -f7);
            if login_shell_is_ok; then return 0; fi
        fi
    fi
    return 1;
}

using_python() {
    if detect_python; then
        output=$($python -c "import pwd, os; print(pwd.getpwuid(os.geteuid()).pw_shell)")
        if [ $? = 0 ]; then 
            login_shell=$output; 
            if login_shell_is_ok; then return 0; fi
        fi
    fi
    return 1
}

execute_with_python() {
    if detect_python; then
        exec $python -c "import os; os.execl('$login_shell', '-' '$shell_name')"
    fi
    return 1;
}

LOGIN_SHELL="OVERRIDE_LOGIN_SHELL"
if [ -n "$LOGIN_SHELL" ]; then
    login_shell="$LOGIN_SHELL"
else
    using_getent || using_id || using_python || using_passwd || die "Could not detect login shell";
fi
shell_name=$(basename $login_shell)

export KITTY_SHELL_INTEGRATION="SHELL_INTEGRATION_VALUE"

exec_bash_with_integration() {
    export ENV="$shell_integration_dir/bash/kitty.bash"
    export KITTY_BASH_INJECT="1"
    exec "$login_shell" "--posix"
}

exec_zsh_with_integration() {
    zdotdir="$ZDOTDIR"
    if [ -z "$zdotdir" ]; then 
        zdotdir=~; 
        unset KITTY_ORIG_ZDOTDIR  # ensure this is not propagated
    else
        export KITTY_ORIG_ZDOTDIR="$zdotdir"
    fi
    # dont prevent zsh-new-user from running
    if [ -f "$zdotdir/.zshrc" -o -f "$zdotdir/.zshenv" -o -f "$zdotdir/.zprofile" -o -f "$zdotdir/.zlogin" ]; then
        export ZDOTDIR="$shell_integration_dir/zsh"
        exec "$login_shell" "-l"
    fi
}

exec_fish_with_integration() {
    if [ -z "$XDG_DATA_DIRS" ]; then
        export XDG_DATA_DIRS="$shell_integration_dir"
    else
        export XDG_DATA_DIRS="$shell_integration_dir:$XDG_DATA_DIRS"
    fi
    export KITTY_FISH_XDG_DATA_DIR="$shell_integration_dir"
    exec "$login_shell" "-l"
}

exec_with_shell_integration() {
    case "$shell_name" in
        "zsh")
            exec_zsh_with_integration
            ;;
        "bash")
            exec_bash_with_integration
            ;;
        "fish")
            exec_fish_with_integration
            ;;
    esac
}

case "$KITTY_SHELL_INTEGRATION" in
    "")
        unset KITTY_SHELL_INTEGRATION
        ;;
    *"no-rc"*)
        ;;
    *)
        exec_with_shell_integration
        unset KITTY_SHELL_INTEGRATION
        ;;
esac

# We need to pass the first argument to the executed program with a leading -
# to make sure the shell executes as a login shell. Note that not all shells
# support exec -a so we use the below to try to detect such shells
if [ -z "$PIPESTATUS" ]; then
    # the dash shell does not support exec -a and also does not define PIPESTATUS
    execute_with_python
    exec $login_shell "-l"
fi
exec -a "-$shell_name" $login_shell
