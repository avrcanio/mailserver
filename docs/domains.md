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
| `uzorita.hr` | Booking / rezervacije — **temporarily disabled** (`mail.uzorita.hr` off cert) | da | ne — use `mail.finestar.hr` |
| `vitalgroupsa.com` | Poslovna | da | da |
| `sibenik1983.hr` | Poslovna | da | da |
| `barakags.hr` | Poslovna (+ `imap.`/`smtp.` SAN) | da | da |
| `stay.hr` | Stay app | da | da |
| `qubitmdm.online` | MDM | da | da |
| `predix.club` | App / noreply | da | ne |
| `roamkit.net` | Roamkit app | da | da |
| `omnimsg.io` | OmniMSG app | da | da |
| `tablio.hr` | Tablio app | da | da |
| `andrijavrcan.from.hr` | Osobna (Andrija) | da | da |
| `antevrcan.from.hr` | Osobna (Ante) | da | da |
| `aurosys.de` | Poslovna | da | da |
| `quede.se` | Poslovna | da | da |

Dodatni cert SAN-ovi: `imap.barakags.hr`, `smtp.barakags.hr`.  
**Temporarily disabled:** `mail.uzorita.hr` — LE DNS-01 NXDOMAIN on `_acme-challenge.mail.uzorita.hr` (2026-08-02). Mailboxi rade; IMAP/SMTP host: `mail.finestar.hr`.

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

### uzorita.hr (temporarily disabled — `mail.uzorita.hr` not on TLS cert)

| Mailbox |
|---------|
| `room_reservations@uzorita.hr` |
| `booking@uzorita.hr` |

Klijenti: `mail.finestar.hr` IMAP `993` / SMTP `587` (ne `mail.uzorita.hr` dok se cert ne popravi).

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
| `info@roamkit.net` | `noreply@`, `support@`, `billing@`, `security@`, `hello@`, `dmarc@`, `staging@`, `mexc@` |

- SMTP/IMAP login: `info@roamkit.net`
- App `DEFAULT_FROM_EMAIL`: `noreply@roamkit.net` (šalje se autentikacijom `info@`, alias je dozvoljen uz `SPOOF_PROTECTION=1`)
- Klijenti: `mail.roamkit.net` IMAP `993` / SMTP `587`

### omnimsg.io

| Mailbox | Aliasi |
|---------|--------|
| `info@omnimsg.io` | `noreply@`, `support@`, `hello@`, `es-smoke@`, `dmarc@`, `abuse@`, `postmaster@` |

- SMTP/IMAP login: `info@omnimsg.io`
- App `DEFAULT_FROM_EMAIL`: `noreply@omnimsg.io`
- Klijenti: `mail.omnimsg.io` IMAP `993` / SMTP `587`

### tablio.hr

| Mailbox | Aliasi |
|---------|--------|
| `info@tablio.hr` | `noreply@`, `support@`, `hello@`, `es-smoke@`, `dmarc@`, `abuse@`, `postmaster@` |

- SMTP/IMAP login: `info@tablio.hr`
- App `DEFAULT_FROM_EMAIL`: `noreply@tablio.hr`
- Klijenti: `mail.tablio.hr` IMAP `993` / SMTP `587`

### andrijavrcan.from.hr

| Mailbox | Aliasi |
|---------|--------|
| `andrija@andrijavrcan.from.hr` | `hello@`, `dmarc@`, `abuse@`, `postmaster@`, `srima@` |

- SMTP/IMAP login: `andrija@andrijavrcan.from.hr`
- Klijenti: `mail.andrijavrcan.from.hr` IMAP `993` / SMTP `587`

### antevrcan.from.hr

| Mailbox | Aliasi |
|---------|--------|
| `ante@antevrcan.from.hr` | `hello@`, `dmarc@`, `abuse@`, `postmaster@` |
| `aurum-systems@antevrcan.from.hr` | — |

- SMTP/IMAP login: puna adresa (lowercase)
- Klijenti: `mail.antevrcan.from.hr` IMAP `993` / SMTP `587`

### aurosys.de

| Mailbox | Aliasi |
|---------|--------|
| `ante.vrcan@aurosys.de` | `info@`, `hello@`, `contact@`, `office@`, `support@`, `sales@`, `billing@`, `admin@`, `noreply@`, `privacy@`, `postmaster@`, `abuse@`, `dmarc@` |
| `andrija.vrcan@aurosys.de` | — |

- SMTP/IMAP login: puna adresa (lowercase)
- Klijenti: `mail.aurosys.de` IMAP `993` / SMTP `587`

### quede.se

| Mailbox | Aliasi |
|---------|--------|
| `avrcan@quede.se` | `info@`, `hi@`, `quede@`, `reserva@`, `hello@`, `contact@`, `office@`, `support@`, `admin@`, `noreply@`, `privacy@`, `postmaster@`, `abuse@`, `dmarc@` |

- SMTP/IMAP login: `avrcan@quede.se`
- Klijenti: `mail.quede.se` IMAP `993` / SMTP `587`

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
