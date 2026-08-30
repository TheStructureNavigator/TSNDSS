# TSN DSS — Mosaic Planner Implementation Plan

## Cel

Zaimplementuj w TSN DSS moduł **Mosaic Planner**, który pozwala użytkownikowi zaplanować wielopanelowe pokrycie wybranego obszaru nieba na podstawie rzeczywistego pola widzenia aktywnego profilu obrazowania.

Najważniejsza zasada:

> **Najpierw przeanalizuj istniejący kod TSN DSS i dopasuj rozwiązanie do obecnej architektury. Nie zakładaj nazw plików, klas, endpointów, komponentów ani technologii, których nie ma w repozytorium.**

Ten dokument opisuje **zachowanie, model domenowy i wymagania funkcjonalne**, a nie narzuca konkretnej implementacji.

## 1. Najpierw analiza istniejącego projektu

Przed wprowadzeniem zmian:

1. Przejrzyj strukturę repozytorium.
2. Znajdź istniejące modele związane z targetami, projektami, sesjami obserwacyjnymi, teleskopem, `TelescopeState`, `ImagingProfile`, FOV / footprintem, Aladin Lite, konfiguracją backendu i frontendem.
3. Znajdź obecny sposób zapisywania danych, generowania ID, serializacji modeli, obsługi API, komunikacji frontend ↔ backend, rysowania overlayów na mapie i przechowywania ustawień.
4. Sprawdź, czy istnieją już funkcje transformacji współrzędnych, rysowania footprintu, obliczania FOV, pracy z RA/Dec oraz obsługi czasu i lokalizacji obserwatora.
5. Wykorzystaj istniejące mechanizmy zamiast tworzyć równoległe rozwiązania.

Po analizie zaproponuj **minimalny zakres zmian**, zgodny z aktualnym stylem projektu.

Nie przebudowuj architektury bez potrzeby.

## 2. Zakres pierwszej wersji

Pierwsza wersja Mosaic Planner ma umożliwiać:

1. zaznaczenie obszaru nieba na mapie,
2. wygenerowanie paneli mozaiki,
3. ustawienie overlapu,
4. zapis planu mozaiki,
5. wybór pojedynczego panelu,
6. pokazanie jego centrum RA/Dec,
7. wizualizację footprintów na Aladinie,
8. śledzenie statusu panelu,
9. przygotowanie panelu jako celu przyszłej obserwacji.

Na tym etapie **nie implementuj automatycznego sterowania Seestarem**, jeśli nie istnieje jeszcze odpowiednia warstwa adaptera.

Mosaic Planner ma działać niezależnie od konkretnego sprzętu.

## 3. Główna koncepcja

Mozaika jest zbiorem paneli pokrywających określony obszar sfery niebieskiej.

Każdy panel ma własny środek RA/Dec, FOV, rotację, overlap wynikający z planu i status realizacji.

Panel jest **konkretnym footprintem na sferze niebieskiej**, a nie tylko komórką siatki UI.

## 4. Model domenowy

Dopasuj nazwy i strukturę do istniejącego modelu TSN DSS.

Nie twórz dokładnie poniższych klas, jeśli obecny kod ma lepsze miejsce lub konwencję.

### MosaicPlan

Logicznie powinien zawierać co najmniej:

```text
id
name
target_id / project_id — jeżeli pasuje do istniejącego modelu
imaging_profile_id
created_at
updated_at

center_ra
center_dec

region_width_deg
region_height_deg

rotation_deg
overlap_percent

status
```

Mozaika powinna być możliwa do zapisania i ponownego otwarcia.

### MosaicPanel

Każdy panel powinien logicznie zawierać:

```text
id
mosaic_id

index / label

center_ra
center_dec

fov_width_deg
fov_height_deg

rotation_deg

row / column — opcjonalnie, wyłącznie jeśli rzeczywiście przydatne

status
target_integration_seconds — opcjonalnie
acquired_integration_seconds — opcjonalnie
```

Panel powinien istnieć niezależnie od aktualnego Alt/Az.

## 5. RA/Dec kontra Alt/Az

Panel przechowuje **RA/Dec** jako trwały adres na sferze niebieskiej.

Nie zapisuj aktualnego Alt/Az jako trwałej właściwości panelu.

Alt/Az jest wartością dynamiczną:

```text
AltAz = f(
    RA,
    Dec,
    observer latitude,
    observer longitude,
    observer elevation,
    observation time
)
```

Jeżeli projekt już używa biblioteki astronomicznej, wykorzystaj ją. Jeżeli nie, wybierz sprawdzoną bibliotekę zamiast implementowania własnych wzorów astronomicznych.

## 6. Generowanie paneli

Wejście:

```text
region center
region width
region height
FOV width
FOV height
rotation
overlap %
```

Overlap oznacza wspólny obszar sąsiednich paneli.

W prostym przybliżeniu:

```text
step_x = FOV_width  * (1 - overlap)
step_y = FOV_height * (1 - overlap)
```

Ale nie traktuj nieba jak zwykłej płaskiej kartki, jeśli prowadziłoby to do istotnych błędów.

Przy małych mozaikach proste przybliżenie może wystarczyć, ale implementacja powinna być przygotowana na poprawne operowanie współrzędnymi sferycznymi. W szczególności ruch w RA zależy od deklinacji.

Codex powinien dobrać odpowiednią metodę po przeanalizowaniu obecnych bibliotek i skali zastosowania.

## 7. Minimalizacja liczby paneli

Planner powinien próbować pokryć cały zaznaczony region przy zadanym overlapie.

Nie generuj paneli poza regionem bez potrzeby.

Jednocześnie pozostaw niewielki margines bezpieczeństwa, jeśli jest potrzebny do późniejszego stitchingu.

Pierwsza wersja może używać regularnej prostokątnej siatki.

Nie implementuj jeszcze skomplikowanej optymalizacji geometrii, jeśli nie jest konieczna.

## 8. Rotacja

Każda mozaika powinna obsługiwać `rotation_deg`.

Rotacja wpływa na orientację footprintu.

Jeśli obecny `ImagingProfile` ma już rotation, wykorzystaj ten mechanizm.

Planner powinien mieć możliwość użycia rotacji profilu i opcjonalnego override dla konkretnej mozaiki.

## 9. Integracja z ImagingProfile

Mosaic Planner nie powinien mieć na stałe wpisanego FOV Seestara.

Powinien korzystać z aktywnego `ImagingProfile`.

Dzięki temu ten sam planner będzie działał później dla Seestara, 72ED, custom riga i innych profili.

## 10. UI — Mosaic Mode

Na istniejącym widoku Aladin dodaj tryb:

```text
Mosaic Mode
```

Minimalny workflow:

1. użytkownik otwiera mapę,
2. wybiera Mosaic Mode,
3. ustawia lub zaznacza region,
4. ustawia overlap,
5. wybiera profil obrazowania,
6. TSN DSS generuje footprinty,
7. użytkownik zatwierdza i zapisuje plan.

Nie kopiuj żadnego przykładowego layoutu 1:1, jeśli nie pasuje do aktualnego UI.

## 11. Zaznaczanie regionu

Preferowany UX:

- przeciągnięcie prostokąta na Aladinie,

lub jeśli obecna integracja Aladina tego nie wspiera w prosty sposób:

- wybór centrum,
- podanie szerokości i wysokości regionu.

W pierwszej wersji ważniejsza jest stabilność niż rozbudowany UX.

## 12. Rendering footprintów

Każdy panel powinien być narysowany na Aladinie.

Powinien mieć widoczny:

- obrys,
- numer / label,
- status,
- zaznaczenie aktywnego panelu.

Przykładowe stany:

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

Dopasuj enum / nazwy do istniejących konwencji.

Kliknięcie footprintu powinno wybierać panel.

## 13. Panel Details

Po wybraniu panelu pokaż jego podstawowe dane:

```text
MOSAIC / PANEL 04

RA
DEC
FOV
Rotation
Status
```

Jeśli TSN DSS posiada już lokalizację obserwatora i czas, można dodatkowo pokazać dynamicznie Current Alt / Current Az, ale nie jest to wymaganie blokujące MVP.

## 14. Wybór panelu jako celu

Panel powinien być możliwy do ustawienia jako aktualny planowany target.

Nie wiąż tego jeszcze bezpośrednio ze sprzętem.

Potrzebujemy semantycznej operacji w rodzaju:

```text
Select Panel for Observation
```

Rezultat:

```text
active planned pointing:
RA = panel.center_ra
DEC = panel.center_dec
```

Później hardware adapter może wykorzystać te dane do slew.

## 15. Przyszła integracja z Telescope Adapter

Nie implementuj jej teraz, jeśli infrastruktura jeszcze nie istnieje.

Docelowy przepływ:

```text
Mosaic Panel
     ↓
RA / Dec
     ↓
Telescope Adapter
     ↓
slew
     ↓
plate solve
     ↓
center
     ↓
tracking
     ↓
acquisition
```

Mosaic Planner nie powinien wiedzieć, czy sprzętem jest Seestar, custom ASCOM rig, simulator czy inny teleskop.

## 16. Status realizacji panelu

Pierwsza wersja może pozwalać na ręczne ustawienie:

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

Jeśli model projektu pozwala naturalnie powiązać panel z sesją / capture / datasetem, zrób takie powiązanie.

Docelowo można liczyć target integration, acquired integration i progress.

## 17. Wielokrotne sesje jednego panelu

Panel nie może być traktowany jako jednorazowa obserwacja.

Powinno być możliwe dokładanie kolejnych sesji do tego samego panelu i sumowanie integracji.

## 18. Filtry

Nie łącz filtra bezpośrednio z geometrią panelu.

Ten sam panel może być obserwowany wielokrotnie z różnymi filtrami, np.:

```text
Panel 04

Dual Band:
7200 s

UV/IR:
3600 s
```

Geometria panelu pozostaje ta sama.

Parametry acquisition należą do sesji / capture / planu obserwacyjnego zgodnie z istniejącą architekturą TSN DSS.

## 19. Rozszerzanie istniejącej mozaiki

Projekt powinien umożliwić w przyszłości rozszerzenie regionu.

Nie zakładaj, że mozaika zawsze jest tworzona raz i później zamknięta.

Panel powinien mieć trwałe RA/Dec.

## 20. Edycja paneli

Nie musi wejść do pierwszego MVP, ale model nie powinien tego uniemożliwiać.

Docelowo użytkownik powinien móc przesunąć panel, usunąć panel, dodać panel ręcznie, zmienić rotację lub ponownie wygenerować fragment siatki.

## 21. Przyszły Observation Planner

Mosaic Planner powinien być zaprojektowany tak, żeby później można było dla każdego panelu obliczyć:

```text
altitude over time
azimuth over time
transit
rise/set
visibility window
Moon separation
```

Nie implementuj całego schedulera w pierwszym etapie.

## 22. Przyszły automatyczny scheduler

Docelowo scheduler może wybierać kolejność paneli na podstawie wysokości, brakującej integracji, statusu, filtra, Księżyca i warunków obserwacyjnych.

To jest poza MVP.

## 23. Przyszły stitching

Mosaic Planner odpowiada za planowanie i identyfikację paneli, nie za sam stitching.

Model danych powinien jednak pozwolić później zbudować pipeline:

```text
RAW frames
   ↓
calibration
   ↓
registration
   ↓
stack per panel
   ↓
panel normalization
   ↓
astrometric alignment
   ↓
mosaic stitching
   ↓
final processing
```

W przypadku wielu filtrów powinny być możliwe osobne mozaiki, np. Dual Band i UV/IR, a dopiero później combined processing.

## 24. Persistencja

Użyj istniejącego systemu przechowywania danych TSN DSS.

Nie twórz drugiego źródła prawdy.

Jeżeli TSN DSS nadal używa SQLite jako source of truth, Mosaic Planner powinien zostać włączony do tego modelu zgodnie z obecną architekturą i mechanizmem migracji.

## 25. API

Nie narzucaj nowych endpointów przed sprawdzeniem obecnego API.

Potrzebne operacje logiczne to:

```text
create mosaic
read mosaic
update mosaic
delete mosaic

generate panels

list panels
read panel
update panel

select active panel
```

Dopasuj sposób komunikacji do repozytorium.

## 26. Walidacja

Waliduj co najmniej:

```text
0 <= overlap < 100
FOV > 0
region width > 0
region height > 0
RA poprawne
Dec w zakresie -90..+90
```

Ustaw rozsądny limit maksymalnej liczby paneli, żeby przypadkowe parametry nie zamroziły UI.

## 27. Testy

Dodaj testy zgodnie z obecnym systemem testowym projektu.

Minimalne przypadki:

- panel generation dla 0%, 10%, 25%, 50% overlap,
- pełne pokrycie regionu,
- zmiana liczby paneli po zmianie ImagingProfile,
- rotation,
- przypadki wokół RA = 0h,
- wysokie dodatnie Dec,
- ujemne Dec,
- persistence,
- ponowne otwarcie planu bez utraty geometrii.

## 28. Przypadek testowy — Cygnus Loop

Jako pierwszy realny scenariusz użyj:

```text
Target:
Cygnus Loop / Veil Nebula

Approximate region:
~3° scale

Profile:
Seestar S30 Pro

Approximate FOV:
2.59° × 1.47°

Overlap:
25%
```

Nie wpisuj na stałe liczby paneli.

Planner ma sam wyliczyć wynik na podstawie rzeczywistej geometrii i ustawień.

Potem sprawdź wizualnie na Aladinie, czy cały region jest pokryty i czy footprinty rzeczywiście nachodzą na siebie.

## 29. Przypadek testowy — Orion

Drugi test powinien używać znacznie większego regionu, np. fragmentu kompleksu Oriona.

Celem jest sprawdzenie dużej liczby paneli, wydajności UI, poprawności siatki, zapisu dużego planu i możliwości wyboru dowolnego panelu.

## 30. UX — podstawowa filozofia

Użytkownik powinien myśleć:

> Chcę sfotografować TEN obszar nieba.

a nie:

> Muszę ręcznie policzyć dziesiątki współrzędnych.

TSN DSS wykonuje geometrię.

Użytkownik:

1. zaznacza region,
2. wybiera instrument,
3. ustawia overlap,
4. generuje plan,
5. wybiera panel,
6. zbiera dane.

## 31. Nie implementować teraz

W pierwszym etapie NIE dodawaj bez wyraźnej potrzeby:

- automatycznego sterowania Seestarem,
- automatycznego acquisition,
- schedulera całej nocy,
- automatycznego stitchingu,
- zaawansowanego weather planningu,
- optymalizatora kolejności paneli,
- AI planowania,
- automatycznego pobierania katalogów astronomicznych,
- własnego silnika plate solvingu.

MVP ma być małe i stabilne.

## 32. Proponowany podział prac

### Phase M1 — Model

- analiza istniejącej architektury,
- model MosaicPlan,
- model MosaicPanel,
- persistence,
- podstawowe CRUD.

### Phase M2 — Geometry Engine

- generowanie footprintów,
- overlap,
- rotation,
- RA/Dec,
- testy pokrycia.

### Phase M3 — Aladin Integration

- render paneli,
- selection,
- active panel,
- panel details.

### Phase M4 — Mosaic UI

- tworzenie planu,
- ustawienie regionu,
- profile,
- overlap,
- save/load.

### Phase M5 — Observation Integration

- możliwość wskazania panelu jako planowanego pointingu,
- powiązanie z istniejącą sesją / projektem, jeśli architektura na to pozwala.

### Phase M6 — Science / Planning Extensions — później

- Alt/Az,
- visibility windows,
- Moon separation,
- scheduler,
- telescope adapter,
- automatic acquisition,
- mosaic stitching.

## 33. Definition of Done dla MVP

MVP Mosaic Planner jest gotowy, jeśli użytkownik może:

1. wejść w Mosaic Mode,
2. wybrać istniejący ImagingProfile,
3. wskazać region nieba,
4. ustawić overlap,
5. wygenerować panele,
6. zobaczyć footprinty na Aladinie,
7. kliknąć dowolny panel,
8. odczytać jego RA/Dec,
9. zapisać mozaikę,
10. zamknąć aplikację,
11. ponownie otworzyć plan,
12. zobaczyć identyczne panele,
13. wybrać panel jako planowany cel obserwacji.

## 34. Ważne zasady implementacyjne

- TSN DSS pozostaje hardware-agnostic.
- RA/Dec jest podstawowym adresem panelu na niebie.
- Alt/Az jest wartością dynamiczną zależną od obserwatora i czasu.
- ImagingProfile określa FOV.
- Mosaic Planner nie może być zaszyty pod Seestara.
- Nie duplikuj istniejących modeli i logiki.
- Nie twórz nowych warstw, jeśli obecna architektura już rozwiązuje problem.
- Zachowaj obecny styl UI.
- Zachowaj istniejące conventions projektu.
- SQLite pozostaje source of truth, jeżeli nadal jest nim w aktualnym kodzie.
- Najpierw analiza repozytorium, potem minimalna propozycja zmian, następnie implementacja.

## 35. Instrukcja dla Codexa przed rozpoczęciem implementacji

Przed napisaniem kodu:

1. Przeczytaj ten plan.
2. Przejrzyj aktualne repozytorium TSN DSS.
3. Zidentyfikuj istniejące elementy, które można wykorzystać.
4. Wskaż pliki/moduły, które rzeczywiście wymagają zmiany.
5. Wskaż minimalne zmiany modelu danych.
6. Sprawdź wpływ zmian na istniejące funkcje.
7. Dopiero wtedy implementuj.

Jeżeli plan używa pojęcia, które w repozytorium ma już inną nazwę lub reprezentację, **dopasuj plan do kodu, a nie kod do dokumentu**.

Jeżeli jakaś część planu koliduje z aktualną architekturą, zachowaj intencję funkcjonalną i wybierz rozwiązanie zgodne z istniejącym projektem.

## Docelowa wizja

```text
SKY REGION
    ↓
MOSAIC PLANNER
    ↓
PANELS (RA / DEC)
    ↓
OBSERVATION PLANNER
    ↓
TELESCOPE ADAPTER
    ↓
ACQUISITION
    ↓
FITS
    ↓
DATASET PER PANEL
    ↓
STACKS
    ↓
MOSAIC
    ↓
FINAL IMAGE / SCIENCE OUTPUT
```

Mosaic Planner jest pierwszym krokiem pomiędzy:

> „chcę obserwować ten obszar nieba”

a:

> „mam kompletny, wielosesyjny dataset całego regionu”.
