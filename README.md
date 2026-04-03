# Docker Mailserver

Produkcijski Docker mail stack za dedicated server s vise domena, direktnim slanjem i primanjem maila te jednostavnim CLI provisioningom.

## Stack

- `docker-mailserver` za SMTP/IMAP, virtual mailboxe, DKIM, DMARC i osnovnu antispam zastitu
- `certbot/dns-cloudflare` kroz helper skriptu za Let’s Encrypt certifikate preko DNS challengea
- bind mount volumeni za mailboxe, state, logove, config i certifikate

## Portovi

- `25` SMTP inbound/server-to-server
- `465` SMTPS
- `587` Submission s autentikacijom
- `993` IMAPS

Traefik se ne koristi za mail promet. Mail portovi idu direktno na host.

## Brzi Start

1. Kopiraj [`.env.example`](/opt/stacks/mailserver/.env.example) u `.env` i popuni stvarne vrijednosti.
2. Kreiraj Cloudflare credentials file:

```bash
cat > docker-data/certbot/secrets/cloudflare.ini <<'EOF'
dns_cloudflare_api_token = REPLACE_ME
EOF
chmod 600 docker-data/certbot/secrets/cloudflare.ini
```

3. Podigni stack:

```bash
docker compose up -d
```

4. Zatrazi prvi certifikat:

```bash
./scripts/certbot-renew.sh
docker compose restart mailserver
```

5. Generiraj DKIM kljuceve:

```bash
./scripts/mail.sh config dkim
```

6. Dodaj prvi mailbox:

```bash
./scripts/mail.sh email add info@example.com 'StrongPasswordHere'
```

## Upravljanje mailboxima

Wrapper skripta [scripts/mail.sh](/opt/stacks/mailserver/scripts/mail.sh) koristi sluzbeni `setup.sh` workflow iz `docker-mailserver`.

Primjeri:

```bash
./scripts/mail.sh email add info@example.com 'StrongPasswordHere'
./scripts/mail.sh email update info@example.com 'NewStrongPasswordHere'
./scripts/mail.sh alias add sales@example.com info@example.com
./scripts/mail.sh relay add contact@example.com external@gmail.com
./scripts/mail.sh debug login info@example.com 'StrongPasswordHere'
```

Dodavanje nove domene bez mailboxa ide preko aliasa ili kreiranjem prvog mailboxa na toj domeni.

DNS zapis predlozak za konkretnu domenu:

```bash
./scripts/render-dns-records.sh example.com
```

## DNS Checklist po domeni

Za svaku domenu postavi:

- `MX 10 mail.<domena>`
- `A mail.<domena> -> 65.108.196.92`
- `TXT @ -> v=spf1 mx a:${MAIL_HOSTNAME} -all`
- `TXT _dmarc -> v=DMARC1; p=quarantine; rua=mailto:dmarc@<domena>`
- `TXT mail._domainkey` prema vrijednosti iz generiranog DKIM kljuca

Napomena:

- jedan server IP treba jedan kanonski `PTR/rDNS`
- `PTR` za `65.108.196.92` treba promijeniti da pokazuje na `${MAIL_HOSTNAME}`
- kanonski hostname treba A zapis natrag na isti IP
- ostali `mail.<domena>` hostovi mogu biti SAN-ovi na istom certifikatu

## Certifikati

Certifikat se obnavlja preko helper skripte [scripts/certbot-renew.sh](/opt/stacks/mailserver/scripts/certbot-renew.sh). Nakon uspjesnog renewala restartaj `mailserver` da DMS ucita novi certifikat.

Rucni poziv:

```bash
./scripts/certbot-renew.sh
docker compose restart mailserver
```

Ako dodas novu domenu u `ADDITIONAL_CERT_DOMAINS`, pokreni:

```bash
./scripts/certbot-renew.sh
docker compose restart mailserver
```

Preporuka za automatski renewal na hostu:

```bash
crontab -e
```

Dodaj:

```cron
17 3,15 * * * cd /opt/stacks/mailserver && ./scripts/certbot-renew.sh && docker compose restart mailserver >/var/log/mailserver-cert-renew.log 2>&1
```

## Operativni zadaci

- status:

```bash
docker compose ps
docker compose logs -f mailserver
```

- backup:

```bash
./scripts/backup.sh
```

- health check:

```bash
./scripts/check-mail-health.sh
```

## Testiranje

Nakon deploya potvrdi:

- `ss -ltnp` pokazuje `25`, `465`, `587`, `993`
- IMAP login radi za stvarni mailbox
- SMTP submission radi s autentikacijom
- inbound mail dolazi izvana
- outbound prolazi na Gmail i Outlook
- `SPF`, `DKIM`, `DMARC` su `pass`
- `PTR` i `HELO` su uskladeni s `${MAIL_HOSTNAME}`

## Buduci Django/API sloj

Ovaj repo namjerno zadrzava mailbox provisioning u datotekama i DMS admin komandama. Kasniji Django admin/API moze koristiti ove skripte ili izvoditi iste `docker-mailserver setup` komande kroz zaseban servisni sloj.
