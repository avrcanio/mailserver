require ["copy", "envelope", "variables", "vnd.dovecot.pipe"];

if envelope :matches "to" "*" {
    set "recipient" "${1}";
}

pipe :copy "fcm-notify" ["${recipient}"];
