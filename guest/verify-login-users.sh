#!/usr/bin/env bash
set -euo pipefail
awk -F: '($3==0 || ($3>=1000 && $3<65534)) && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | sort
