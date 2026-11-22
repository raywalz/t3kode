#!/bin/sh
# Boot a Kobo straight into KOReader instead of Nickel's home screen.
#
# Install (as root over SSH, with KOReader in /mnt/onboard/.adds/koreader):
#   mkdir -p /usr/local/koreader-autostart
#   cp autostart.sh /usr/local/koreader-autostart/ && chmod +x /usr/local/koreader-autostart/autostart.sh
#   cp 99-koreader-autostart.rules /etc/udev/rules.d/
#
# Skip it for one boot or for good: create /mnt/onboard/.adds/koreader/no-autostart
# (over USB or SSH). Undo: delete the rules file and /usr/local/koreader-autostart.
# Exiting KOReader still brings Nickel back, as it does when KFMon starts it.
# A firmware update may remove the rules file; copy it back afterwards.

KOREADER_DIR="/mnt/onboard/.adds/koreader"
DONE_FLAG="/tmp/koreader-autostart.done"
LOG="/tmp/koreader-autostart.log"
export PATH="/sbin:/usr/sbin:/bin:/usr/bin:${PATH}"

if [ "$1" != "--detached" ]; then
    # udev may send this event again later; only act once per boot (/tmp is a tmpfs).
    [ -e "${DONE_FLAG}" ] && exit 0
    touch "${DONE_FLAG}"
    # udev kills slow RUN programs, so wait in a new session and return at once.
    setsid "$0" --detached </dev/null >/dev/null 2>&1 &
    exit 0
fi

{
    # Wait for Nickel and the user partition (up to 2 minutes).
    i=0
    while ! { pkill -0 nickel && [ -x "${KOREADER_DIR}/koreader.sh" ]; }; do
        i=$((i + 1))
        [ "${i}" -gt 120 ] && { echo "gave up waiting for Nickel" >>"${LOG}"; exit 0; }
        sleep 1
    done

    if [ -e "${KOREADER_DIR}/no-autostart" ]; then
        echo "skipped: ${KOREADER_DIR}/no-autostart exists" >>"${LOG}"
        exit 0
    fi

    # Let Nickel finish starting up (its DB, Wi-Fi, plugins) before KOReader stops it.
    sleep 10

    # Don't start a second copy if KOReader is already up (e.g. launched by hand).
    if pkill -0 -f reader.lua; then
        echo "skipped: KOReader already running" >>"${LOG}"
        exit 0
    fi

    echo "starting KOReader $(date)" >>"${LOG}"
    cd "${KOREADER_DIR}" && exec ./koreader.sh >>"${LOG}" 2>&1
}
