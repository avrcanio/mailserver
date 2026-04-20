# Docker Mailserver

Produkcijski Docker mail stack za dedicated server s vise domena, direktnim slanjem i primanjem maila te jednostavnim CLI provisioningom.

## Stack

- `docker-mailserver` za SMTP/IMAP, virtual mailboxe, DKIM, DMARC i osnovnu antispam zastitu
 - Django `mailadmin` portal za interni mail ops, blocklist CRUD i operativne akcije
- `postgres` za spremanje blocklist pravila
- `certbot/dns-cloudflare` kroz helper skriptu za Let’s Encrypt certifikate preko DNS challengea
- bind mount volumeni za mailboxe, state, logove, config i certifikate

## Portovi

- `25` SMTP inbound/server-to-server
- `465` SMTPS
- `587` Submission s autentikacijom
- `993` IMAPS

Traefik se ne koristi za mail promet. Mail portovi idu direktno na host.

## Brzi Start

1. Kopiraj [`.env.example`](/opt/stacks/mailserver/.env.example) u `.env` i popuni stvarne vrijednosti, ukljucujuci `CLOUDFLARE_DNS_API_TOKEN`.
2. Podigni stack:

```bash
docker compose up -d
```

Interni Django mail admin ce nakon toga biti dostupan lokalno na:

```text
http://127.0.0.1:8081
```

Predvidena poddomena za reverse proxy/TLS izlaganje je:

```text
https://${MAILADMIN_HOST}
```

Prijava ide preko Django admin korisnika iz lokalnog `.env`.

Primjer host-level reverse proxy konfiguracije za `mailadmin.finestar.hr` nalazi se u [docs/mailadmin-nginx.conf.example](/opt/stacks/mailserver/docs/mailadmin-nginx.conf.example).

3. Zatrazi prvi certifikat:

```bash
./scripts/certbot-renew.sh
docker compose restart mailserver
```

4. Generiraj DKIM kljuceve:

```bash
./scripts/mail.sh config dkim
```

5. Dodaj prvi mailbox:

```bash
./scripts/mail.sh email add info@example.com 'StrongPasswordHere'
```

Sigurnosna napomena:

- `CLOUDFLARE_DNS_API_TOKEN` ostaje samo u lokalnom `.env`
- token koji je vec bio javno podijeljen treba rotirati odmah nakon prvog uspjesnog izdavanja certifikata
- `scripts/certbot-renew.sh` generira privremeni `cloudflare.ini` samo tijekom izvrsavanja i brise ga pri izlazu

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

### Automatsko kreiranje mailboxa iz mailadmin usera

`mailadmin` moze automatski kreirati `docker-mailserver` mailbox kada se u Django adminu kreira obican non-staff user.
Feature je opt-in. U `.env` ukljuci:

```bash
MAILBOX_AUTO_CREATE_FROM_USER_ADMIN=true
MAILBOX_AUTO_CREATE_SKIP_STAFF=true
```

Zatim restartaj samo mailadmin:

```bash
docker compose up -d mailadmin
```

Operativni tok:

1. Otvori `https://${MAILADMIN_HOST}/admin/`.
2. Idi na `Authentication and Authorization` -> `Users` -> `Add user`.
3. Upisi `username`, `email`, `password` i `password confirmation`.
4. Spremi usera bez `staff` ili `superuser` oznake.
5. `mailadmin` ce pozvati `setup email add <email> <password>` u `mailserver` kontejneru.
6. Provjeri mailbox:

```bash
docker exec mailserver setup email list
./scripts/mail.sh debug login user@example.com 'PasswordFromAdmin'
```

Napomene:

- raw password se koristi samo tijekom create requesta i ne sprema se u Django model
- ako mailbox provisioning padne, Django user creation se rollbacka
- staff/superuser accounti se preskacu
- v1 ne sinkronizira kasnije promjene emaila ili passworda; mailbox password mijenjaj preko `./scripts/mail.sh email update ...`

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

Certifikat se obnavlja preko helper skripte [scripts/certbot-renew.sh](/opt/stacks/mailserver/scripts/certbot-renew.sh). Skripta cita Cloudflare token iz lokalnog `.env`, generira privremeni credentials file samo za trajanje certbot procesa i nakon uspjesnog renewala treba restartati `mailserver` da DMS ucita novi certifikat.

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

- mailadmin:

```bash
docker compose logs -f mailadmin
```

- mail index sync runner:

```bash
docker compose logs -f mailindex-sync
docker compose exec mailadmin python manage.py run_mail_index_sync_cycle
```

- Gmail import:

```bash
docker compose logs -f gmail-import-sync
docker compose exec mailadmin python manage.py run_gmail_import --help
```

Setup, OAuth bootstrap, historical import, incremental sync, and smoke-test
commands are documented in [docs/gmail-import.md](/opt/stacks/mailserver/docs/gmail-import.md).

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

## Django Mailadmin Portal

`mailadmin` je interni Django-based mail ops portal pripremljen za poddomenu `mailadmin.finestar.hr`.

V1 ukljucuje:

- Django admin login i interni dashboard
- CRUD za `sender_email` i `sender_domain` blocklist pravila
- rucni `Apply to Postfix`
- pregled zadnjeg apply rezultata i greske

Operativni tok:

1. Otvori `http://127.0.0.1:8081/admin/` ili reverse proxied `https://${MAILADMIN_HOST}/admin/`
2. Prijavi se Django admin korisnikom iz `.env`
3. U adminu dodaj ili izmijeni `Sender blocklist rules`
4. Otvori dashboard na `/`
5. Klikni `Apply to Postfix`
6. Potvrdi u logovima da je Postfix reloadan i da se poruke odbijaju na ulazu

Portal je pripremljen i za kasnije sirenje prema:

- mailbox upravljanju
- alias i relay pravilima
- DNS i DKIM operacijama
- health, reject i auth observability pregledima

Interni mailbox JSON endpointi za staff korisnike dokumentirani su u [docs/mailbox-api.md](/opt/stacks/mailserver/docs/mailbox-api.md).

Provjera odbijanja:

```bash
docker compose logs -f mailserver
```

Trazi `reject` dogadaj za blokiranog posiljatelja. Ocekivano je da mail bude odbijen tokom SMTP sesije i da se ne pojavi ni u inboxu ni u spamu.
