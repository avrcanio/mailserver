## Onboarding nove domene (multi-domain mailserver)

Ovaj dokument je “checklist” za dodavanje nove domene na postojeći `docker-mailserver` (isti IP/host, više domena).

### 0) Kanonski identitet servera (reputacija)

- **PTR/rDNS** za server IP treba pokazivati na **kanonski hostname** (npr. `mail.finestar.hr`).
- Kanonski hostname mora imati **A record** natrag na isti IP (FCrDNS).
- Ne mijenjati kanonski hostname/PTR bez jasnog razloga (najviše utječe na reputaciju).

### 1) DNS zapisi za novu domenu (Cloudflare)

Pretpostavka: koristimo “vitalgroupsa style”, tj. `mail.<domena>` pokazuje na IP mailservera.

- **A**
  - `mail.<domena>` → `65.108.196.92` (DNS only)
- **MX**
  - `<domena>` → `mail.<domena>` (prio 10)
- **CNAME (preporučeno za klijente)**
  - `autoconfig.<domena>` → `mail.<domena>`
  - `autodiscover.<domena>` → `mail.<domena>`
- **SPF (TXT na root)**
  - `v=spf1 mx a:mail.<domena> ~all` (tijekom warmup-a)
  - kasnije po potrebi pooštriti u `-all`
- **DMARC (TXT na `_dmarc`)**
  - početno: `v=DMARC1; p=quarantine; rua=mailto:dmarc@<domena>; adkim=s; aspf=s`
  - kad je stabilno: razmotriti `p=reject`
- **DKIM (TXT na `mail._domainkey`)**
  - vrijednost dolazi iz generiranog ključa (vidi korak 4)

### 2) Certifikat (SAN za `mail.<domena>`)

Mailserver koristi Let’s Encrypt cert za kanonski `MAIL_HOSTNAME` (CN) i SAN listu:

- Dodaj `mail.<domena>` u `ADDITIONAL_CERT_DOMAINS` u lokalnom `.env` / `mailserver.env` (gitignored)
- U `mailserver.env` (lokalni, gitignored) postavi `ADDITIONAL_CERT_DOMAINS` na **sve postojeće SAN-ove** + novi `mail.<domena>` (inače certbot `--expand` može ispustiti stare).
- Token: `CLOUDFLARE_DNS_API_TOKEN` ili `CLOUDFLARE_DNS_API_TOKEN_FILE` (npr. Traefik allzones token).
- Pokreni:

```bash
cd /opt/stacks/mailserver
./scripts/certbot-renew.sh
docker compose up -d --force-recreate mailserver
```

Napomena:
- DNS-01 preko Cloudflare zna trebati >10s. Skripta koristi default 60s.
- Nakon DKIM generiranja koristi `--force-recreate` (ne samo `restart`), da `_setup_opendkim` synca KeyTable/SigningTable i ključeve u `/etc/opendkim`.

### 3) Postfix: prihvati inbound domenu

DMS FILE provisioner automatski puni `/etc/postfix/vhost` iz mailbox accounta. Nakon prvog `email add` na novoj domeni, domena treba biti u:

```bash
docker exec mailserver cat /etc/postfix/vhost
```

Ako domena nije na listi, inbound može završiti kao `Relay access denied` — reload/restart:

```bash
docker exec mailserver postfix reload
# ili
docker compose restart mailserver
```

Napomena: ne koristimo više statički `postfix-main.cf` override za `virtual_mailbox_domains`; autoritativan je auto `vhost`.

### 4) Kreiraj mailbox(e)

Primjer:

```bash
cd /opt/stacks/mailserver
./scripts/mail.sh email add user@<domena> 'StrongPasswordHere'
./scripts/mail.sh email list
```

### 5) DKIM (obavezno za DMARC alignment kad se koristi SRS)

Ovaj stack koristi SRS (`postsrsd`), pa se envelope-from često prepisuje (npr. u `@finestar.hr`).
U tom slučaju SPF može biti “unaligned” s `From:` domenom, pa je **DKIM** ključan da DMARC prođe.

- Generiraj DKIM za novu domenu:

```bash
cd /opt/stacks/mailserver
./scripts/mail.sh config dkim domain '<domena>'
```

- Objavi TXT iz:
  - `docker-data/dms/config/opendkim/keys/<domena>/mail.txt`
  u Cloudflare kao `mail._domainkey.<domena>`

- Provjeri da su tablice ažurirane:
  - `docker-data/dms/config/opendkim/SigningTable` ima `*@<domena> mail._domainkey.<domena>`
  - `docker-data/dms/config/opendkim/KeyTable` ima entry za `<domena>`

- Primijeni (restart `opendkim` ili cijeli mailserver):

```bash
docker exec mailserver supervisorctl restart opendkim
```

### 6) Mailadmin user (opcionalno, operativa)

Ako je uključeno auto-provisioning iz mailadmin-a, kreiranje non-staff usera može pokušati dodati mailbox koji već postoji.
U tom slučaju kreiraj mailadmin usera kao **staff** (da se provisioning preskoči), ili prvo kreiraj user pa mailbox.

### 7) Test plan (obavezno)

- **Inbound**: Gmail/Outlook → `user@<domena>` (mora doći u INBOX).
- **Outbound**: `user@<domena>` → Gmail/Outlook.
- Gmail “Show original” mora pokazati:
  - `spf=pass`
  - `dkim=pass (d=<domena>)`
  - `dmarc=pass (header.from=<domena>)`

Ako je `dmarc=fail` a vidiš `Return-Path` (SRS) na drugoj domeni, to znači da DKIM nije aktivan/učitan za tu domenu.

