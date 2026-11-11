# Bot Discord – weryfikacja podatku ze screena

Komenda `/podatek` przyjmuje screen, odczytuje go przez OCR (Tesseract) i zwraca wynik.
Na ekranie finansów gry sprawdza stan konta oraz czy wśród pięciu widocznych ostatnich
transakcji jest ujemna transakcja co najmniej równa 12% stanu konta. Obsługuje też
dotychczasowe odczyty IRS Online Account / Tax Transcript. Bota obsługujesz z okna (`gui.py`).
Raport każdego sprawdzenia trafia do osobnego kanału Discord skonfigurowanego przez właściciela aplikacji.

## Pliki

| Plik | Do czego |
|---|---|
| `gui.py` | Panel desktopowy: konfiguracja, uruchamianie, OCR, dziennik i pomoc PL/EN |
| `bot.py` | Sam bot (można go odpalić bez GUI: `python bot.py`) |
| `core.py` | OCR, parser kwot, zapis ustawień do `config.json` |
| `requirements.txt` | Zależności Pythona |

## 1. Instalacja programów

1. **Python 3.10+** (na Windows zaznacz przy instalacji „Add Python to PATH”).
   Okno `gui.py` używa tkintera: na Windows/macOS jest w pakiecie, na Linuxie: `sudo apt install python3-tk`.
2. **Tesseract OCR** (osobny program, nie pakiet pip):
   - Windows: https://github.com/UB-Mannheim/tesseract/wiki (domyślna ścieżka to zwykle
     `C:\Program Files\Tesseract-OCR\tesseract.exe`)
   - Linux: `sudo apt install tesseract-ocr`
   - macOS: `brew install tesseract`
3. W folderze z plikami: `pip install -r requirements.txt`

## 2. Podpięcie bota do Discorda

1. Wejdź na https://discord.com/developers/applications → **New Application** → nadaj nazwę.
2. Zakładka **General Information** → skopiuj **Application ID**.
3. Zakładka **Bot** → **Reset Token** → skopiuj token (Discord pokazuje go tylko raz).
   Żadnych „Privileged Gateway Intents” nie trzeba włączać.
4. Zaproś bota na swój serwer (musisz mieć tam uprawnienie *Zarządzanie serwerem*):
   - w oknie bota wpisz Application ID i kliknij **Link zaproszenia**, albo
   - w portalu: **OAuth2 → URL Generator**, zaznacz scope `bot` i `applications.commands`,
     otwórz wygenerowany link i wybierz serwer.
5. (Zalecane) Włącz tryb dewelopera: Discord → Ustawienia użytkownika → Zaawansowane →
   **Tryb dewelopera**. Potem kliknij prawym na ikonę serwera → **Kopiuj ID serwera**.
   Z ID serwera komenda `/podatek` pojawia się od razu; bez niego dopiero po ok. godzinie.

Jeśli podczas startu pojawi się `403 Missing Access (50001)`, sprawdź, czy ID serwera
w GUI jest ID serwera, na którym ma działać bot, i czy zaproszono na niego tę samą
aplikację bota ze scope `bot` oraz `applications.commands`. W razie potrzeby ponownie
otwórz link zaproszenia i dodaj bota na właściwy serwer. Możesz też wyczyścić pole
**ID serwera** i zapisać ustawienia — wtedy komendy będą synchronizowane globalnie
i mogą pojawić się z opóźnieniem do około godziny.

## 3. Uruchomienie

1. `python gui.py`
2. Wpisz token, Application ID, ID serwera, a jeśli Tesseract nie jest w PATH – jego ścieżkę
   (**Przeglądaj...**).
3. **Zapisz ustawienia**, potem **▶ Start bota**. Status zmieni się na „działa jako ...”.
   Panel oferuje zakładki Przegląd, Konfiguracja, OCR, Pomoc i wsparcie oraz Dziennik.
   Język interfejsu (polski/angielski) można zmienić w prawym górnym rogu; wybór jest
   zapisywany w `config.json`.
   Jeśli pole ścieżki Tesseract pozostanie puste, bot spróbuje znaleźć program w PATH
   oraz w standardowych folderach instalacyjnych Windows.
4. Na serwerze, jako właściciel aplikacji bota, wpisz `/ustaw_kanal_podatkow` i wybierz
   tekstowy kanał raportowy. Bot musi mieć na nim uprawnienie do wysyłania wiadomości.
   Ta komenda jest zablokowana dla pozostałych osób.
5. Jako właściciel aplikacji wpisz `/ustaw_kanal_firm` i wybierz kanał tabeli.
   Bot potrzebuje tam uprawnień do wysyłania wiadomości i osadzania linków.
6. W kanale należącym do kategorii firmy administrator używa `/aktywna` albo `/nieaktywna`.
   Nazwa kategorii staje się nazwą firmy; typowe dopiski, np. „Firma”, „Company” i formy
   prawne spółek, są pomijane w wyświetlanej nazwie. `/aktywna` zapisuje też ten kanał jako
   miejsce wysyłania przypomnień. Tylko administratorzy mogą zmieniać status.
   Administrator może najpierw użyć `/szkic`; po potwierdzeniu bot odtworzy kategorie
   i kanały widoczne na czterech screenach oraz utworzy rolę dla każdej firmy. Kategorie
   firm będą widoczne tylko dla właściwej roli, administratorów i bota. Administrator
   musi ręcznie przypisać członków do ról firmy.
   `/szkic2` tworzy prywatną kategorię `DOJ` i role `IRS` oraz `DOJ`. Tylko te role,
   administratorzy i bot widzą tę kategorię; kanały `tabela` i `logi` są tylko do odczytu,
   a `chat` pozwala pisać. Bot automatycznie podłącza `tabela` do tablic firm i kar,
   a `logi` do raportów. Członków należy przypisać do ról IRS/DOJ ręcznie.
   `/wyczysc` wymaga uprawnienia **Zarządzanie wiadomościami** i dodatkowego
   potwierdzenia. Usuwa wszystkie wiadomości bota z bieżącego kanału, również przypięte;
   wiadomości użytkowników pozostają.
   Administrator może użyć `/wyczysc_tabele`, aby usunąć opublikowane wiadomości
   tabel firm i kar. Komenda nie kasuje danych rejestru ani konfiguracji kanału;
   tabele zostaną opublikowane ponownie przy następnym odświeżeniu.
7. Wpisz `/podatek` w kanale tej samej kategorii i dołącz screen. Odpowiedź dla osoby
   wywołującej pokazuje tylko status podatku, datę, nazwę firmy i stan konta.
   Jeśli OCR nie odczyta stanu konta i transakcji, bot poprosi o kolejny, wyraźniejszy screen.
   W kanale raportowym bot publikuje spójny, kolorowy raport z wynikiem, wymaganym
   podatkiem, stanem konta, wykrytą transakcją, datą i osobą przesyłającą screen.
   Stała tabela w kanale firm aktualizuje status aktywności i zapłatę podatku. Obrazy nie są zapisywane.
   Odczyt działa na całym obrazie, próbuje kilku wariantów kontrastu i rozpoznawania
   układu oraz obsługuje m.in. etykiety „stan konta”, „saldo”, „balans” i „balance”,
   separatory tysięcy, zapis dziesiętny oraz kilka formatów dat. Nagłówki tabel
   „Data / Kwota” nie są traktowane jako część opisu transakcji. Nie gwarantuje odczytu każdego projektu
   ekranu — przy niepewnym wyniku bot poprosi o inny, czytelniejszy screen.
   Zakładka **OCR** pozwala lokalnie sprawdzić screen i zobaczyć rozpoznany tekst,
   saldo, transakcje, pewność OCR oraz powód decyzji. W konfiguracji wybierz profil
   screena (`Ogólny / automatyczny`, `Panel finansów` lub `Karta transakcji`) i ustaw
   dla niego język OCR (`eng`, `pol` lub `eng+pol`) oraz układ (`auto`, `dashboard`
   lub `transaction-card`). Profile są zapisywane oddzielnie w `config.json`; profil
   można też wskazać w komendach `/podatek` i `/oplac_kare`. Tesseract musi mieć
   zainstalowany wybrany plik językowy. Na ekranie karty transakcji, który nie
   pokazuje salda, OCR może odczytać samą transakcję, ale nie może wyliczyć z niej
   podatku bez salda.
   Każdy członek serwera może zgłosić screen komendą `/podatek` lub `/oplac_kare`
   w kanale kategorii firmy i zatwierdzić własny odczyt. Dopasowanie po kwocie nie
   wystarcza do pewnego uznania przelewu za podatek lub karę: niejednoznaczny wynik
   wymaga kliknięcia przez osobę zgłaszającą. Osoba z rolą weryfikacji wpłat lub
   administrator może dodatkowo zatwierdzić albo odrzucić publicznie widoczne
   zgłoszenie podatku innego członka; prywatny podgląd widzi tylko autor.
   Automatyczne potwierdzenie jest zarezerwowane dla czytelnego opisu wskazującego
   podatek/karę oraz wysokiej pewności OCR.
8. Administratorzy mogą używać dodatkowych komend:
   - `/ustaw_role_irs typ rola` ustawia rolę **Weryfikacja wpłat** albo
     **Zarządzanie karami**. Wybierz typ i rolę; pominięcie pola roli usuwa
     przypisanie dla wybranego typu. Administratorzy zawsze zachowują pełny dostęp.
     Każdy członek może zgłosić wpłatę przez `/podatek` i `/oplac_kare` oraz
     zatwierdzić własny odczyt. Rola weryfikacji może zatwierdzać lub odrzucać
     publicznie widoczne zgłoszenia podatku innych osób; prywatny podgląd widzi
     tylko autor. Rola zarządzania karami może używać `/kara_finansowa`,
     `/rejestr_kar`, `/eksport_kar` i `/usun_kare`. Gdy rola danego typu nie jest
     ustawiona, zarządzanie karami pozostaje dostępne tylko administratorom;
     zgłoszenia płatności nadal mogą wykonywać wszyscy członkowie serwera.
   - `/pomoc` wyświetla instrukcję po polsku lub angielsku, zgodnie z językiem Discorda użytkownika.
   - `/szkic`, `/szkic2` oraz `/wyczysc` są opisane w kroku uruchomienia. Tworzenie
     układu i czyszczenie kanału zawsze wymagają potwierdzenia.
   - `/przypomnienie` wysyła wiadomość na zapisane kanały aktywnych firm, które nie mają
     potwierdzonej zapłaty ani ulgi. Kanał przypomnienia jest zapisywany przez `/aktywna`.
   - `/kara_finansowa` w kanale kategorii firmy przyjmuje kwotę, powód i termin
     (`RRRR-MM-DD`), zapisuje karę w rejestrze i publikuje raport z jej statusem oraz
     wyraźnym identyfikatorem kary.
     Osobę nakładającą bot odczytuje z Discorda.
   - `/rejestr_kar` pokazuje do 20 najnowszych wpisów z danego serwera osobom
     z rolą zarządzania karami i administratorom.
     Kara ma status **oczekująca**, **po terminie** (ustalany automatycznie po dacie terminu)
     albo **opłacona**.
   - `/eksport_kar` wysyła pełny rejestr kar jako prywatny plik CSV
     do arkusza kalkulacyjnego.
   - `/oplac_kare kara_id screen` działa w kategorii firmy objętej karą. Każdy członek
     serwera może zgłosić wpłatę i zatwierdzić własny wynik. Użytkownik
     dołącza screen finansów lub listy transakcji; OCR sprawdza kwotę i opis. Dla tej
     komendy saldo konta nie musi być widoczne. Niejednoznaczna transakcja pokazuje
     prywatny podgląd i zmienia rejestr dopiero po zatwierdzeniu; brak pasującej
     transakcji powoduje prośbę o kolejny screen. Zapisuje osobę potwierdzającą oraz czas.
   - `/usun_kare` usuwa wpis po podaniu numeru z rejestru i dodatkowym potwierdzeniu.
   - `/audyt` pokazuje do 20 ostatnich działań administratorów na bieżącym serwerze;
     wpisy zawierają też nazwę firmy, gdy działanie jej dotyczy.
     Dziennik obejmuje zmianę aktywności firmy, ulgę, reset podatków, przypomnienia,
     nałożenie/opłacenie/usunięcie kary, usunięcie firmy i uruchomienie prezentacji. Każdy wpis zawiera
     wykonawcę, czas i opis zmiany.
   - `/ulga` w kanale kategorii zwalnia tę firmę z podatku do kolejnego resetu tygodnia;
     ulga jest widoczna w tabeli i wyklucza firmę z przypomnień.
   - `/muala` resetuje potwierdzenia zapłaty i ulgi wszystkich firm na serwerze.
     Aktywność firm oraz rejestr kar pozostają bez zmian. Reset jest ręczny.
   - `/usun_firme` w kanale kategorii usuwa firmę z tablicy po dodatkowym potwierdzeniu.
     Wpisy kar tej firmy pozostają w rejestrze.
   - `/prezentacja` najpierw pokazuje ostrzeżenie i wymaga potwierdzenia. Tworzy kategorię
     oraz tylko kanał `irs-demo-start`. Po kliknięciu **Rozpocznij demo** i kolejnych
     przycisków **Dalej** tworzy po kolei kanały firm, podatków, kar i raportów; każdy
     krok zawiera krótkie wiadomości wprowadzające, przykłady, odpowiedź na najczęstsze
     pytanie oraz przycisk **Co zrobić dalej?**. Przykładowe firmy/kary są statyczne, nie trafiają do rejestru,
     a komendy produkcyjne są blokowane w kategorii demo. Postęp jest zapisywany i przyciski
     bieżącego kroku działają po restarcie bota.
   - `/zakoncz_prezentacje` po dodatkowym potwierdzeniu usuwa wyłącznie kanały i kategorię
     zarejestrowane przez demo. Kanały przeniesione lub nieśledzone są zachowywane, a błędy
     sprzątania nie usuwają zapisu — komendę można ponowić. Pokaz nie zmienia istniejących
     firm, kar ani konfiguracji; jego uruchomienie i zakończenie trafiają do audytu.
     Kanały dziedziczą standardowe uprawnienia serwera, więc ich widoczność zależy od ról.

W kanale tabeli firm bot publikuje również oddzielną tabelę aktywnych kar. Tabela firm
pokazuje liczbę i kwoty nieopłaconych kar. W tabeli kar znajdują się kary oczekujące,
po terminie oraz opłacone w ciągu ostatnich 24 godzin; po tym czasie opłacone wpisy
znikają z tabeli, ale pozostają w rejestrze kar. Tabela jest odświeżana przy zmianach
i cyklicznie co kilka minut.

Po upływie terminu każda nieopłacona kara jest oznaczana jako przeterminowana.
Bot wysyła embed z jej ID, nazwą firmy, kwotą i terminem na kanale firmy oraz,
jeśli skonfigurowano kanał raportowy, także tam. W dzienniku aplikacji zapisuje
jedno ostrzeżenie dla każdej kary. Stan wysyłki do każdego kanału jest zapamiętany
osobno, dzięki czemu nieudane powiadomienia mogą być ponawiane bez powielania
tych, które dotarły.

Dzień przed terminem płatności bot wysyła przypomnienie o niezapłaconej karze na
kanale firmy i kanale raportowym. Znacznik wysyłki dla obu miejsc jest przechowywany
osobno. Raz w tygodniu bot publikuje na kanale raportowym zestawienie kar nałożonych
i wpłaconych w poprzednim tygodniu oraz bieżących kar oczekujących i po terminie.
Jeśli kanał raportowy nie jest dostępny, bot ponawia próbę w kolejnych cyklach.

Tablica pokazuje bieżący okres od niedzieli 00:00 do następnej niedzieli 00:00.
Zakres dat jest informacyjny — statusy podatkowe nadal resetuje administrator ręcznie
komendą `/muala`.

Kwoty wysyłane przez bota są formatowane skrótowo po polsku, np. `1 tys. $`,
`1 mln $` i `1 mld $`; starsze wpisy kar również są tak wyświetlane.

Przycisk **Wybierz screen i sprawdź odczyt...** testuje OCR lokalnie, bez Discorda – dobry
sposób, żeby dopasować parser do swoich screenów.

Obsługiwane są m.in. układy panelu z saldem i listą transakcji oraz karta pojedynczej
transakcji bez salda (np. opis firmy, numer konta, obciążenie i czas „Dziś o 14:30”).
Numery kont są pomijane przy odczycie kwot. Dla panelu z saldem i opisem ogólnym,
np. „Przelew z firmy…”, system wymaga ręcznego potwierdzenia — sama kwota i data nie
dowodzą, że była to wpłata podatku. Pewność OCR opisuje jakość rozpoznanego tekstu,
nie pewność klasyfikacji płatności.

Zakładka **Pomoc i wsparcie** zawiera checklistę konfiguracji, skróconą instrukcję komend,
rozwiązywanie typowych problemów, odnośnik do dokumentacji i funkcję kopiowania bezpiecznego
raportu diagnostycznego. Raport nie zawiera tokena ani identyfikatorów serwera/aplikacji.

Bot działa tylko wtedy, gdy okno jest otwarte i komputer włączony. Do pracy 24/7 uruchom
`python bot.py` na serwerze/VPS (token przez zmienną `DISCORD_TOKEN`, opcjonalnie `GUILD_ID`
i `TESSERACT_CMD`).

## Bezpieczeństwo

- Token trafia do `config.json` jako zwykły tekst. Nie udostępniaj tego pliku ani tokena.
  Jeśli token wycieknie, zrób **Reset Token** w portalu.
- Odpowiedź na `/podatek` domyślnie widzi tylko autor komendy. `publicznie:True` pokazuje te cztery pola wszystkim.
- Nie wrzucaj screenów z numerem SSN. Bot nie zapisuje obrazów, ale Discord je przechowuje.
- Status firmy i dane tabeli są przechowywane lokalnie w `companies.sqlite3`.
- Baza przechowuje także kanał przypomnień, identyfikatory wiadomości obu tabel oraz
  rejestr kar finansowych; aktualizuje się automatycznie po uruchomieniu, bez utraty
  dotychczasowych danych. Migracja dodaje pola opłacenia i identyfikator tabeli kar,
  a osobny dziennik audytu zachowuje historię działań administratorów także po usunięciu
  firmy lub kary.

## Dostosowanie

Parser IRS używa słów kluczowych w `PAID_KEYWORDS` i `BALANCE_KEYWORDS` w `core.py`.
Parser ekranu finansów analizuje tekst z całego obrazu, a następnie szuka etykiety salda
i widocznych transakcji niezależnie od ich stałego położenia. Karta transakcji może nie
zawierać salda — w takim przypadku nie jest używana do automatycznego wyliczenia 12%.
Zachowuje też awaryjny
odczyt starszego układu ekranu. OCR nadal może pomylić cyfry lub nie rozpoznać
niestandardowego interfejsu — sprawdź wynik z obrazem i prześlij kolejny screen,
jeśli odczyt jest niepełny lub niepewny.
