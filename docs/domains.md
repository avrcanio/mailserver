# Domene i mailboxi na mailserveru

Inventar produkcijskog multi-domain stacka (`docker-mailserver` + `mailadmin`).

**Kanonski hostname:** `mail.finestar.hr`  
**Server IP:** `65.108.196.92` (PTR → `mail.finestar.hr`)  
**Provjera live stanja:**

```bash
docker exec mailserver setup email list
docker exec mailserver setup alias list
docker exec mailserver cat /etc/postfix/vhost
```

Napomena: lozinke i hashovi su u `postfix-accounts.cf` (gitignored). Domene se prihvaćaju automatski preko FILE accounts → `/etc/postfix/vhost` (nema ručnog `postfix-main.cf` overridea).

---

## Sažetak domena

| Domena | Uloga | DKIM | Cert SAN `mail.<domena>` |
|--------|--------|------|---------------------------|
| `finestar.hr` | Kanonska / primarna | da | da (CN) |
| `dalekopro.hr` | Poslovna | da | da |
| `uzorita.hr` | Booking / rezervacije | da | da |
| `vitalgroupsa.com` | Poslovna | da | da |
| `sibenik1983.hr` | Poslovna | da | da |
| `barakags.hr` | Poslovna (+ `imap.`/`smtp.` SAN) | da | da |
| `stay.hr` | Stay app | da | da |
| `qubitmdm.online` | MDM | da | da |
| `predix.club` | App / noreply | da | ne |
| `roamkit.net` | Roamkit app | da | da |

Dodatni cert SAN-ovi: `imap.barakags.hr`, `smtp.barakags.hr`.

---

## Mailboxi i aliasi po domeni

### finestar.hr

| Mailbox | Aliasi |
|---------|--------|
| `avrcan@finestar.hr` | `abuse@`, `postmaster@`, `hello@` |
| `t_supe@finestar.hr` | — |
| `avrcanus@finestar.hr` | — |
| `review@finestar.hr` | — |
| `app-test-1@finestar.hr` | — |
| `app-test-2@finestar.hr` | — |

### dalekopro.hr

| Mailbox |
|---------|
| `avrcan@dalekopro.hr` |
| `iklaric@dalekopro.hr` |
| `mklaric@dalekopro.hr` |
| `mvukman@dalekopro.hr` |
| `mico@dalekopro.hr` |
| `radovi@dalekopro.hr` |

### uzorita.hr

| Mailbox |
|---------|
| `room_reservations@uzorita.hr` |
| `booking@uzorita.hr` |

### vitalgroupsa.com

| Mailbox |
|---------|
| `ante@vitalgroupsa.com` |
| `andrija@vitalgroupsa.com` |
| `carolina@vitalgroupsa.com` |

### sibenik1983.hr

| Mailbox |
|---------|
| `narudzbe@sibenik1983.hr` |
| `ante@sibenik1983.hr` |
| `frane@sibenik1983.hr` |

### barakags.hr

| Mailbox | Aliasi |
|---------|--------|
| `info@barakags.hr` | — |
| `office@barakags.hr` | `ured@` |
| `anschluss@barakags.hr` | — |

### stay.hr

| Mailbox |
|---------|
| `stay@stay.hr` |
| `privacy@stay.hr` |
| `channex@stay.hr` |
| `superclean@stay.hr` |
| `reports@stay.hr` |

### qubitmdm.online

| Mailbox |
|---------|
| `postmaster@qubitmdm.online` |

### predix.club

| Mailbox | Aliasi |
|---------|--------|
| `noreply@predix.club` | `dmarc@` |

### roamkit.net

| Mailbox | Aliasi |
|---------|--------|
| `info@roamkit.net` | `noreply@`, `support@`, `billing@`, `security@`, `hello@`, `dmarc@` |

- SMTP/IMAP login: `info@roamkit.net`
- App `DEFAULT_FROM_EMAIL`: `noreply@roamkit.net` (šalje se autentikacijom `info@`, alias je dozvoljen uz `SPOOF_PROTECTION=1`)
- Klijenti: `mail.roamkit.net` IMAP `993` / SMTP `587`

---

## DNS obrazac (Cloudflare)

Za svaku novu domenu (DNS only, ne proxied za mail):

| Tip | Ime | Vrijednost |
|-----|-----|------------|
| A | `mail.<domena>` | `65.108.196.92` |
| MX | `<domena>` | `mail.<domena>` (prio 10) |
| TXT | `@` | `v=spf1 mx a:mail.<domena> -all` |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:dmarc@<domena>; adkim=s; aspf=s` |
| TXT | `mail._domainkey` | iz `opendkim/keys/<domena>/mail.txt` |
| CNAME | `autoconfig` / `autodiscover` | `mail.<domena>` |

Helper: `./scripts/render-dns-records.sh <domena>`  
Checklist: [domain-onboarding.md](domain-onboarding.md)

---

## Portovi i klijentski pristup

| Port | Uloga |
|------|--------|
| 25 | SMTP server-to-server |
| 465 | SMTPS |
| 587 | Submission (auth) |
| 993 | IMAPS |

Traefik se ne koristi za mail promet. Username je uvijek puna adresa (`user@domena`).
