## Flutter: prikaz prevedenog HTML maila (zadržava slike i layout)

Backend endpoint:

- `POST /api/mail/messages/<uid>/translate`

Response sada uz `translated_text` vraća i:

- `translated_html` (HTML string; isti layout + slike, ali prevedeni vidljivi tekst)

### 1) DTO promjene

Proširi translation response model da uključuje:

- `translatedHtml: String` (mapirano iz `translated_html`)

Backend i dalje vraća i:

- `translated_text` (plain text fallback)
- `cached`, `truncated`, `source_language`, `target_language`

### 2) UI pravilo prikaza (najvažnije)

U “message detail” ekranu uvedi toggle (ili button) **“Prikaži prijevod”**.

Kad je toggle uključen:

- Ako `translatedHtml` nije prazan:
  - renderaj **`translatedHtml`** u istom HTML widgetu koji koristiš za original (webview/html renderer)
  - očekivanje: slike i linkovi rade kao i u originalu
- Inače:
  - renderaj `translated_text` kao plain text (fallback)

Kad je toggle isključen:

- renderaj originalni `html_body` (postojeće ponašanje).

### 3) Target language (odabir jezika)

Minimalno:

- slati `target_language: "hr"`

Bolje:

- dodati postavku u appu (dropdown) i spremiti lokalno
- koristiti kao `target_language`.

### 4) Timeout i UX (newsletter može trajati)

Prevođenje HTML-a može trajati dulje od kratkih mailova.

- HTTP client timeout stavi na **>= 60s** za translate poziv.
- UI: pokaži loading state i omogući cancel/back bez blokiranja cijelog ekrana.

### 5) “cached / truncated” indikatori

- Ako `cached == true`: prikaži “cached”
- Ako `truncated == true`:
  - prikaži upozorenje “Dio poruke nije preveden zbog duljine.”

### 6) Error handling (poruke korisniku)

Najčešće:

- `503 translation_unavailable` → “Prijevod trenutno nije dostupan.”
- `502 translation_failed` → “Prijevod nije uspio. Pokušaj ponovno.”
- `400 empty_message_body` → “Nema sadržaja za prevesti.”
- `401 mailbox_credentials_missing` → “Nedostaju mailbox podaci. Ponovno se prijavi.”

U error stanju ponudi **Retry**.

### 7) Test plan (ručno)

- Otvori HTML newsletter (s slikama) i okini prijevod.
- Provjeri:
  - da se tekst prevede
  - da slike ostanu vidljive
  - da linkovi vode na iste URL-ove kao original
- Ponovi translate → mora biti brže i `cached == true`.

