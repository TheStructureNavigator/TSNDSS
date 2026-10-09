# Seestar S30 Pro — dziennik badań i odkryć (08.10.2026)

**Projekt:** S30Lab / TSN DSS / WZRD  
**Charakter dokumentu:** raport badawczy, zapis eksperymentów i ustaleń technicznych  
**Urządzenie:** Seestar S30 Pro, identyfikator w sieci `s30pro_79651ce4`, firmware `9.31` (`firmware_ver_int=2931`)  
**Środowisko:** Windows, CMD, Python 3.12.2, środowisko `venv-seestarpy`, biblioteka `seestarpy 0.7.1`, NumPy, Pillow, OpenCV (`opencv-python-headless`)  
**Katalog roboczy:** `C:\Users\treze\OneDrive\Desktop\TSNWorkspace`  
**IP teleskopu w badanej sesji:** `10.160.154.146` (sieć hotspot telefonu; adres sesyjny, nie stała konfiguracja)

> **Zasada wiarygodności:** [POTWIERDZONE] oznacza bezpośredni wynik logu, kodu lub obserwacji użytkownika; [HIPOTEZA] — możliwe wyjaśnienie wymagające testu; [NIEUSTALONE] — brak wystarczających danych. Raport obejmuje informacje z dostępnego zapisu dzisiejszej sesji, w tym wcześniejsze kroki zachowane w kontekście. Nie odtwarza nieudokumentowanych czynności ani ich dokładnej kolejności.

## 1. Cel sesji

Celem było uzyskanie programowego dostępu do kamery szerokokątnej (WIDE, `SecondView`) Seestar S30 Pro, pobranie klatek w Pythonie i rozpoczęcie budowy WZRD: systemu obserwacji nieba, oceny zachmurzenia i docelowo estymacji ruchu chmur oraz pola wektorowego. Dodatkowy cel: zrozumienie różnic między surowymi danymi sensora a kolorowym podglądem w aplikacji producenta.

## 2. Początkowe przygotowanie i konfiguracja

### 2.1. Połączenie urządzenia

- [POTWIERDZONE] Seestar był osiągalny pod `10.160.154.146`; biblioteka zgłaszała `Found Seestar S30 Pro`.
- [POTWIERDZONE] W `get_device_state()` widniało połączenie Wi-Fi typu station z hotspotem telefonu, a urządzenie raportowało firmware `9.31`.
- [POTWIERDZONE] Komendy wykonywano z aktywnego `venv-seestarpy` w katalogu `TSNWorkspace`.
- [POTWIERDZONE] Zainstalowano biblioteki umożliwiające obsługę strumienia i obrazów: `seestarpy`, NumPy, Pillow i OpenCV.
- [POTWIERDZONE] Do autoryzowanych wywołań RPC wykorzystywano istniejący lokalny plik klucza `seestar_alp\firmware-cache\interop.pem` i `auth.set_key_path(...)`.
- [BEZPIECZEŃSTWO] Klucz prywatny nie powinien być publikowany, kopiowany do repozytorium ani dołączany do raportów. Raport nie zawiera jego zawartości.

### 2.2. Włączenie kamery WIDE

- [POTWIERDZONE] Kamerę WIDE aktywowano za pomocą oficjalnej aplikacji na telefonie.
- [POTWIERDZONE] W zdarzeniach aplikacji pojawiły się m.in. `SelectCamera selected_cam=SecondView`, `SecondView state=working,cam_id=1,mode=star` oraz `ContinuousExposure state=working,route=['SecondView']`; główny `View` pozostawał w stanie `Sleep`.
- [POTWIERDZONE] Próba traktowania ustawienia `wide_cam` jako przełącznika aktywnego strumienia nie przyniosła oczekiwanego skutku; późniejszy stan urządzenia pokazywał `setting.wide_cam=false`, mimo dostępności obrazu WIDE.
- [AKTUALIZACJA — POTWIERDZONE] W późniejszym eksperymencie uruchomiono WIDE w trybie Scenery całkowicie bez aplikacji telefonu. Nie było konieczne ustawienie `selected_cam=SecondView`: obie kamery pracowały jednocześnie w trybie RTSP. Dokładne komendy i dowody podano w sekcji 15.

### 2.3. Tryby fotografowania podczas sesji

Początkowo w aplikacji był używany **Milky Way**. Teleskop był skierowany na jasną scenę wewnętrzną (kuchnia), a surowe obrazy były silnie prześwietlone. Następnie urządzenie przeniesiono w inne miejsce i użytkownik przełączył aplikację na **Scenery**. Podgląd Scenery wyglądał poprawnie w telefonie, ale wcześniejsza ścieżka RAW nadal oddawała te same piksele. Dopiero przejście na RTSP dało bieżący obraz zewnętrznej sceny.

**Ważne:** powiązanie prześwietlenia z Milky Way jest bardzo prawdopodobne, lecz nie potwierdzono odczytem rzeczywistego czasu ekspozycji i gainu dla tamtej klatki.

## 3. Odkrycie ścieżki RAW WIDE — TCP 4804

### 3.1. Odczyt

Wykorzystano `seestarpy.stream.get_live_image(ip='10.160.154.146', port=4804, method='get_current_img', fallback=False, read_timeout=15)`.

- [POTWIERDZONE] Biblioteka udostępnia `IMAGE_PORT_WIDE=4804`.
- [POTWIERDZONE] Bez argumentu `filename` metoda zwraca `(header, payload)`.
- [POTWIERDZONE] Nagłówek przykładowej klatki: `magic=963`, `version=2`, `header_size=80`, `img_type=1`, `data_type=3`, `frame_id=2`, `width=2160`, `height=3840`, `can_debayer=1`.
- [POTWIERDZONE] Dekompresja `stream._decompress_payload(payload)` dawała **16 588 800 bajtów** = `2160 × 3840 × 2`, czyli jedną 16-bitową próbkę na piksel.
- [POTWIERDZONE] Obraz można interpretować jako `np.frombuffer(raw, dtype='<u2').reshape(3840, 2160)`.
- [POTWIERDZONE] Standardowa ścieżka `decode_payload` w badanej wersji biblioteki nie obsługiwała poprawnie tego skompresowanego Bayera jako gotowego RGB; obejściem było jawne rozpakowanie i interpretacja jako `uint16`.

Przykład odczytu diagnostycznego:

```python
from seestarpy import stream
import numpy as np

header, payload = stream.get_live_image(
    ip='10.160.154.146', port=4804,
    method='get_current_img', fallback=False, read_timeout=15
)
raw = stream._decompress_payload(payload)
frame16 = np.frombuffer(raw, dtype='<u2').reshape(header['height'], header['width'])
```

**Uwaga:** `_decompress_payload` jest funkcją prywatną biblioteki i może się zmienić w kolejnych wersjach.

### 3.2. Wygenerowane obrazy

| Plik w `S30Lab` | Znaczenie |
|---|---|
| `wide_raw16.png` | Jednokanałowy zapis wartości RAW 16-bit |
| `wide_preview.png` | Podgląd szarości po rozciągnięciu zakresu |
| `wide_color_test.png` | Wstępna, niepełna rekonstrukcja kanałów Bayera; dominacja zieleni |
| `wide_color.png` | Demosaicing OpenCV z `COLOR_BayerGR2RGB`; wyraźny turkus/cyjan |
| `wide_bayer_comparison.png` | Porównanie czterech interpretacji Bayera |
| `wide_whitebalance.png` | Demosaicing + gray-world white balance |
| `wide_warm.png` | Próba ocieplenia i mapowania tonalnego; obraz nadal silnie prześwietlony |
| `wide_raw16_new.png` | Kolejny zapis RAW po przeniesieniu teleskopu |
| `wide_scenery_raw16.png` | RAW pobrany po przełączeniu aplikacji na Scenery; identyczny z poprzednim plikiem |

### 3.3. Bayer i kolory

- [POTWIERDZONE] `get_device_state().result.second_camera.debayer_pattern` raportuje `GR`; również kamera główna raportuje `GR`.
- [POTWIERDZONE] Przetestowano `COLOR_BayerRG2RGB`, `GR2RGB`, `BG2RGB`, `GB2RGB`; warianty dawały istotnie różne dominanty barwne. `GR2RGB` dawał turkusowe zabarwienie.
- [POTWIERDZONE] Próba globalnego wyrównania średnich RGB dała średnie `[37187.7, 52079.832, 48851.293]` i współczynniki `[1.2380333, 0.8840199, 0.942444]`.
- [POTWIERDZONE] `wide_whitebalance.png` nadal miał nieprawidłowe kolory (m.in. różowe jasne obszary i cyjanowe cienie).
- [POTWIERDZONE] Próba ręcznego ocieplenia kanałów i zastosowania krzywej tonalnej (`wide_warm.png`) nie rozwiązała problemu; jasne fragmenty pozostały przepalone.
- [NIEUSTALONE] Nie zrekonstruowano dokładnego pipeline'u ISP producenta: poziomu czerni, korekcji matrycy, transformacji barw, balansu bieli, mapowania tonalnego ani kolejności operacji.

**Wniosek:** nie należy interpretować nienaturalnych kolorów wyłącznie jako błędu wzorca Bayera. W tym eksperymencie istotnym problemem było również nasycenie RAW.

## 4. Diagnostyka nasycenia RAW

### 4.1. Statystyki czterech pozycji Bayera

Przy założeniu układu GRBG, dla `wide_raw16.png`:

| Pozycja | Minimum | P1 | Mediana | P99 | Maksimum |
|---|---:|---:|---:|---:|---:|
| G1 | 8768 | 12288 | 65472 | 65472 | 65472 |
| R | 7936 | 10496 | 65472 | 65472 | 65472 |
| B | 5824 | 7296 | 33216 | 65472 | 65472 |
| G2 | 9216 | 12224 | 65472 | 65472 | 65472 |

- [POTWIERDZONE] W trzech z czterech pozycji Bayera mediana osiągnęła maksymalną zaobserwowaną wartość `65472`.
- [POTWIERDZONE] W późniejszej klatce `wide_raw16_new.png` minimum wynosiło `10624`, maksimum `65472`, a udział pikseli równych `65472` wynosił **77,69%**.
- [POTWIERDZONE] Po przełączeniu na Scenery ponowny odczyt RAW dawał dokładnie te same statystyki.
- [HIPOTEZA] `65472` reprezentuje poziom saturacji/obcięcia danych w tym formacie; bez specyfikacji sensora nie należy automatycznie utożsamiać tej wartości z fizycznym pełnym zakresem ADC.

**Wniosek:** silnie nasycony RAW nie jest dobrym materiałem do rekonstrukcji naturalnego koloru. Utraconych wskutek obcięcia informacji nie da się odzyskać samym balansem bieli.

## 5. Odkrycie nieaktualnej klatki na porcie RAW

### 5.1. Identyczne pliki po zmianie trybu

Porównano SHA-256 plików `wide_raw16_new.png` i `wide_scenery_raw16.png`:

```text
IDENTICAL: True
OLD SHA256: 7429f63b6480b6db49ef06fbefc2154db7f5d1a589d9130bf04a023180afe6d0
NEW SHA256: 7429f63b6480b6db49ef06fbefc2154db7f5d1a589d9130bf04a023180afe6d0
```

- [POTWIERDZONE] Oba zapisane PNG były identyczne bajt po bajcie.
- [POTWIERDZONE] Trzy kolejne pobrania dawały `FRAME=2`, `IMAGE=675`, ale różne hashe **skompresowanych payloadów**.
- [POTWIERDZONE] Po dekompresji i porównaniu próbek odległych o 3 sekundy wynik wyniósł `PIXELS CHANGED: 0.0%`, `SATURATION: 77.69%`, `FRAME IDS: 2 2`.

**Wniosek:** zmienny hash danych transportowych nie dowodzi zmiany obrazu. Należy porównywać rozpakowane piksele lub ich hash.

### 5.2. Próba aktywnej sesji TCP

Zidentyfikowano:

```text
IMAGE_PORT_WIDE = 4804
start_stream(ip=None, port=4800, on_image=None, with_matplotlib=False)
StreamSession(ip, port, sock, on_image)
get_live_image(ip=None, port=4800, method='get_stacked_img', ...)
```

Inspekcja kodu `start_stream()` wykazała, że otwiera połączenie TCP, wysyła `begin_streaming`, uruchamia wątki odbioru i heartbeat, a na końcu zwraca `StreamSession`, który można zatrzymać przez `.stop()`.

Test 10-sekundowy na porcie 4804:

```text
FRAME: 1 ID: 2 IMAGE: 0   BYTES: 4
FRAME: 2 ID: 2 IMAGE: 675 BYTES: 16588800
FRAME: 3 ID: 2 IMAGE: 0   BYTES: 17
FRAME: 4 ID: 2 IMAGE: 0   BYTES: 17
TOTAL FRAMES: 4
```

- [POTWIERDZONE] Po `begin_streaming` odebrano jeden pełnowymiarowy payload RAW oraz krótkie komunikaty.
- [NIEUSTALONE] Nie ustalono, dlaczego RAW nie aktualizował się w tym trybie, czy wymaga osobnej komendy akwizycji, ani czy port 4804 w ogóle jest przeznaczony do bieżącego Scenery RAW.
- [WAŻNE] Nie stwierdzono awarii samej kamery WIDE: równolegle aplikacja telefonu wyświetlała poprawny bieżący obraz.

## 6. Odczyt konfiguracji firmware

Wywołanie `raw.get_device_state()` potwierdziło m.in.:

| Pole | Odczyt |
|---|---|
| `device.product_model` | `Seestar S30 Pro` |
| `device.firmware_ver_string` | `9.31` |
| `setting.wide_cam` | `false` |
| `setting.wide_4k` | `true` |
| `setting.preview.image_transfer_mode` | `Raw` |
| `setting.second_camera.preview.image_transfer_mode` | `Raw` |
| `setting.second_camera.preview.rtsp_width` | `1080` |
| `setting.second_camera.preview.rtsp_height` | `1920` |
| `setting.second_camera.manual_exp` | `false` |
| `setting.second_camera.isp_exp_ms` | `-999000.0` |
| `setting.second_camera.isp_gain` | `-9990.0` |
| `setting.second_camera.isp_range_gain` | `[0, 600]` |
| `setting.second_camera.isp_range_exp_us_scenery` | `[30, 1000000]` |
| `setting.second_camera.exp_ms.continuous` | `1000` |
| `second_camera.chip_size` | `[2160, 3840]` |
| `second_camera.pixel_size_um` | `1.6` |
| `second_camera.focal_len` | `6.0` |
| `second_camera.fnumber` | `5.0` |
| `second_camera.debayer_pattern` | `GR` |
| `second_camera.gain` | `200` |

Interpretacja:

- [POTWIERDZONE] `manual_exp=false` wskazuje na brak włączonego ręcznego trybu ekspozycji w ustawieniach WIDE.
- [NIEUSTALONE] Ujemne `isp_exp_ms` i `isp_gain` mogą być wartościami sentinel, ale ich semantyka nie została zweryfikowana; **nie wolno traktować ich jako zmierzonych czasów ekspozycji lub wzmocnienia**.
- [POTWIERDZONE] Ustawienia zawierają konfigurację podglądu RTSP, co skierowało badania ku odrębnej ścieżce wideo.

## 7. Kluczowe odkrycie: RTSP WIDE na porcie 4555

Inspekcja `seestarpy.stream` ujawniła `RTSP_PORT`, `RTSP_PORT_WIDE` i `build_rtsp_url`.

```text
RTSP_PORT_WIDE: 4555
build_rtsp_url(ip=None, port=4554)
rtsp://10.160.154.146:4555/stream
```

Dokumentacja funkcji w kodzie biblioteki wskazywała, że jest to URL wykorzystywany przez oficjalną aplikację do podglądu. W tej sesji nie analizowano ruchu sieciowego aplikacji, więc jest to informacja pochodząca z biblioteki, nie niezależna weryfikacja protokołu aplikacji.

### 7.1. Pierwszy skuteczny odczyt kolorowej klatki

```python
import cv2
url = 'rtsp://10.160.154.146:4555/stream'
cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
ok, frame = cap.read()
if ok:
    cv2.imwrite(r'S30Lab\wide_scenery_rtsp.png', frame)
cap.release()
```

Wynik:

```text
CONNECTED: True
FRAME RECEIVED: True
SHAPE: (1920, 1080, 3)
SAVED: True
```

- [POTWIERDZONE] Odebrano kolorową klatkę w formacie BGR OpenCV o rozmiarze **1920 pikseli wysokości × 1080 pikseli szerokości**.
- [POTWIERDZONE] Użytkownik obejrzał `wide_scenery_rtsp.png` i potwierdził, że wygląda **tak samo jak obraz w aplikacji telefonu**.
- [POTWIERDZONE] Obraz przedstawiał zewnętrzną scenę: zachmurzone niebo, budynek, drzewa i jasne źródło światła przy górnej krawędzi.
- [WNIOSKOWANIE] RTSP jest w tym środowisku praktycznym źródłem bieżącego kolorowego obrazu do WZRD. Nie oznacza to, że zastępuje RAW w zastosowaniach wymagających liniowej fotometrii.

### 7.2. Test aktualizacji klatek

Odczyt dwóch kolejnych klatek z odstępem `sleep(3)`:

```text
FRAMES OK: True True
PIXELS CHANGED: 8.3 %
```

- [POTWIERDZONE] Obie klatki odebrano, a 8,3% pozycji pikseli różniło się co najmniej jednym kanałem.
- [ZASTRZEŻENIE] Ten test nie mierzy udziału poruszających się chmur. Zmiany mogą obejmować kompresję, szum, ruch obiektów, zmiany ekspozycji oraz opóźnienia/buforowanie `VideoCapture`.

## 8. Pierwsza sekwencja akwizycji WZRD

Zapisano pięć klatek `S30Lab/wzrd_01.png`–`S30Lab/wzrd_05.png` z jednej sesji RTSP, z przerwami `time.sleep(3)` pomiędzy odczytami.

```text
CONNECTED: True
FRAME 1 SAVED: True
FRAME 2 SAVED: True
FRAME 3 SAVED: True
FRAME 4 SAVED: True
FRAME 5 SAVED: True
```

Wszystkie obrazy miały rozmiar `(1920, 1080, 3)`.

### 8.1. Analiza różnic w skali szarości

| Para klatek | Średnia bezwzględna różnica jasności | Udział pikseli z różnicą > 10 |
|---|---:|---:|
| 1 → 2 | 0.305 | 0.10% |
| 2 → 3 | 0.375 | 0.16% |
| 3 → 4 | 0.339 | 0.17% |
| 4 → 5 | 0.334 | 0.18% |

- [POTWIERDZONE] Kolejne obrazy różniły się, ale różnice jasności były niewielkie.
- [NIEUSTALONE] Nie ustalono rzeczywistych znaczników czasowych ekspozycji każdej klatki ani stopnia opóźnienia bufora RTSP.

## 9. Pierwszy eksperyment optical flow

### 9.1. Metoda

- Porównano `wzrd_01.png` i `wzrd_05.png`.
- Konwersja do skali szarości.
- Algorytm `cv2.calcOpticalFlowFarneback` z parametrami `(pyr_scale=0.5, levels=3, winsize=31, iterations=5, poly_n=7, poly_sigma=1.5, flags=0)`.
- Analizowano górne **40% wysokości obrazu** (`h=int(1920*0.4)=768`), co okazało się niewystarczające do całkowitego wyeliminowania koron drzew.
- Wektory próbkowano na siatce co 60 px od pozycji 40 px.
- Rysowano strzałki dla długości przepływu `>0.15 px`, powiększając je **20×** wyłącznie na potrzeby wizualizacji.
- Zapisano `S30Lab/wzrd_flow.png`.

Wynik:

```text
SAVED: S30Lab/wzrd_flow.png
VECTORS: 4
MEAN FLOW: 0.0163 px
```

### 9.2. Oględziny mapy

- [POTWIERDZONE] Na pokazanej mapie cztery żółte strzałki znajdowały się przy granicy koron drzew i nieba, a nie na rozległych strukturach zachmurzenia.
- [HIPOTEZA] Wektory mogą wynikać z ruchu liści i gałęzi, które mają wyraźniejsze krawędzie niż chmury; alternatywnie mogą częściowo odzwierciedlać szum, kompresję lub niestabilność estymacji przy krawędziach.
- [POTWIERDZONE] Nie uzyskano wiarygodnego pola wektorowego ruchu chmur.
- [WNIOSKOWANIE] Do dalszych testów niezbędna jest maska nieba/horyzontu oraz większy odstęp czasowy między klatkami.

## 10. Wnioski architektoniczne dla WZRD

### 10.1. Rozdzielić dwa rodzaje akwizycji

**Ścieżka A — RTSP, priorytet na obecnym etapie:** bieżący kolorowy podgląd, szybka segmentacja nieba, wizualizacja, analiza ruchu, wstępna detekcja chmur. Port 4555, `cv2.VideoCapture`, rozdzielczość 1080 × 1920 (szer. × wys.).

**Ścieżka B — RAW, dalsze badania:** potencjalnie wartościowa do pomiarów liniowych i fotometrii, ale obecny odczyt TCP 4804 jest nieaktualny w trybie Scenery. Wymaga rozpoznania protokołu, rzeczywistego sterowania ekspozycją i kalibracji Bayer/ISP.

Nie należy łączyć tych dwóch strumieni w jednym abstrakcyjnym typie bez oznaczenia ich charakterystyki i statusu przetwarzania.

### 10.2. Minimalne metadane akwizycji

Proponowane pola rekordu klatki (projekt, nie wdrożony kontrakt):

- `capture_id`, `device_model`, `device_id`, `firmware_version`
- `source` (`rtsp_wide` / `raw_wide`), `camera` (`SecondView`)
- `host_received_at_utc`, opcjonalnie `device_timestamp`, `sequence_number`
- `mode` (`Scenery`, `Milky Way` itd.; tylko gdy wiarygodnie potwierdzony)
- `width`, `height`, `pixel_format`, `bit_depth`, `color_space`
- `exposure_us`, `gain`, `auto_exposure` (z informacją o źródle i jakości danych)
- `saturation_fraction`, `frame_hash`, `decode_status`
- `orientation`, `sky_mask_version`, `calibration_version`

Nie należy wpisywać domyślnych wartości ekspozycji jako zmierzonych. Brak informacji musi być jawny (`unknown`).

### 10.3. Przetwarzanie sceny

Zalecany pipeline badawczy:

1. Odbiór i buforowanie RTSP z kontrolą świeżości klatek.
2. Rejestracja czasów i kontrola duplikatów.
3. Segmentacja **sky / non-sky**; wykluczenie budynków, drzew, lamp i refleksów.
4. Osobna maska obszarów prześwietlonych i słabo teksturowanych.
5. Normalizacja oświetlenia i pomiar jakości obrazu.
6. Estymacja ruchu tylko w wiarygodnych obszarach nieba.
7. Wizualizacja wektorów wraz z miarą pewności.
8. Dopiero po kalibracji czasu, geometrii i ewentualnej wysokości chmur: interpretacja prędkości i prognoza ich przemieszczenia.

**Nie wolno** interpretować pikseli na sekundę jako metrów na sekundę bez modelu geometrii, odległości/wysokości warstwy chmur i poprawnej kalibracji czasu.

## 11. Otwarte pytania

1. [ROZWIĄZANE CZĘŚCIOWO] Jak uruchamiać `SecondView` / `Scenery` z poziomu RPC, bez telefonu? **Uruchomienie potwierdzone** (sekcja 15); odrębne przełączanie `selected_cam` nie jest wymagane do odbioru RTSP, ale mechanizm zmiany tego pola pozostaje nieustalony.
2. Dlaczego port RAW 4804 oddawał klatkę o `frame_id=2`, `image_id=675`, mimo aktualnego podglądu Scenery?
3. Czy RAW można pobierać na żywo inną metodą/komendą lub po zmianie stanu akwizycji?
4. Jaki jest rzeczywisty pipeline ISP i poziom czerni kamery WIDE?
5. Jak odczytywać rzeczywisty czas ekspozycji i gain bieżącej klatki?
6. Jakie są rzeczywiste FPS, latencja i zachowanie bufora RTSP 4555?
7. [POTWIERDZONE W JEDNEJ SESJI] Czy RTSP można odbierać bez aktywnej aplikacji telefonu? Tak, potwierdzono klatkę i zapis po starcie przez RPC (sekcja 15). Niezawodność po wielu restartach pozostaje do sprawdzenia.
8. Czy parametry jakości RTSP można konfigurować i czy istnieje wyższa rozdzielczość podglądu?
9. Jak uzyskać odporną maskę nieba przy drzewach, lampach, budynkach i zmiennym oświetleniu?
10. Jak rozróżniać ruch chmur od ruchu gałęzi, szumu i artefaktów kompresji?
11. Jaki interwał klatek jest optymalny dla różnych typów i prędkości chmur?
12. Jak zweryfikować pole wektorowe na sekwencji z wyraźnymi, rzeczywiście przemieszczającymi się strukturami chmur?

## 12. Zalecany następny eksperyment

**Cel:** sprawdzić ruch chmur na klatkach RTSP bez wpływu drzew.

- Zachować aktualny, działający tor RTSP 4555.
- Zebrać serię z wiarygodnymi czasami odbioru, np. co 5–10 s przez 2–5 minut; nie polegać wyłącznie na `sleep`, bo `VideoCapture` może buforować obraz.
- Zapisać klatki i metadane do jednego katalogu sesji.
- Wykonać wstępną maskę nieba i osobno oznaczyć lampę, drzewa i budynek.
- Porównać klatki odległe o około 30–60 s; w razie potrzeby dobrać odstęp do obserwowanej prędkości chmur.
- Uruchomić optical flow wyłącznie na obszarze nieba, pokazać strzałki i mapę wiarygodności.
- Sprawdzić stabilność kierunku między kilkoma parami klatek, a nie wyciągać wniosków z pojedynczego wyniku.

## 13. Chronologia najważniejszych kamieni milowych

| Etap | Odkrycie / wynik |
|---|---|
| Konfiguracja | Windows + Python `venv-seestarpy`, Seestar S30 Pro osiągalny po Wi-Fi |
| Aktywacja WIDE | Oficjalna aplikacja uruchamia `SecondView`; potwierdzenie w zdarzeniach |
| RAW 4804 | Odebrano 16-bitową klatkę Bayera 2160 × 3840 |
| Obrazy RAW | Zapis PNG, grayscale, próby debayeringu i balansu bieli |
| Diagnostyka | Silne nasycenie RAW; dla późniejszej klatki 77,69% pikseli przy 65472 |
| Zmiana sceny/trybu | Po przejściu na Scenery odczyt RAW nadal identyczny |
| Weryfikacja | Identyczny SHA-256 plików; rozpakowane piksele 0,0% zmian |
| Streaming TCP | `begin_streaming` na 4804 zwrócił jedną pełną klatkę i krótkie komunikaty |
| Konfiguracja | Odczyt firmware i parametrów `second_camera` |
| RTSP 4555 | Udany odczyt kolorowego obrazu 1920 × 1080 |
| Walidacja wizualna | Użytkownik potwierdził zgodność obrazu RTSP z aplikacją |
| Test żywości | Dwie klatki RTSP: 8,3% pozycji pikseli zmienionych |
| Pierwsza sesja WZRD | Zapis pięciu klatek z RTSP |
| Różnice obrazu | Średnie różnice jasności 0,305–0,375 |
| Optical flow | 4 strzałki, średni przepływ 0,0163 px; strzałki przy koronach drzew |
| Autonomiczne rozłożenie ramienia | `scope_move_to_horizon()` z autoryzacją RSA; `mount.close=false`, `move_type=none` |
| Autonomiczny Scenery WIDE | `iscope_start_view` z `mode=scenery`, `cam_id=1`; `SecondView.stage=RTSP`, port 4555 |
| Autonomiczna klatka | `FRAME: True`, `SHAPE: (1920, 1080, 3)`, `SAVED: True`; `wide_autonomous.png` |

## 14. Stan końcowy dnia

### Osiągnięte

- [x] Programowe połączenie z Seestar S30 Pro.
- [x] Identyfikacja kamery szerokokątnej i jej parametrów.
- [x] Pobranie i zapis surowej klatki Bayera WIDE.
- [x] Zdiagnozowanie nasycenia i nieaktualności odczytu RAW w badanym trybie.
- [x] Odkrycie działającego kolorowego RTSP WIDE (`4555`).
- [x] Zapis aktualnego obrazu zgodnego z aplikacją.
- [x] Zapis pięcioklatkowej sekwencji obserwacyjnej.
- [x] Pierwsza analiza różnic obrazu i demonstracja optical flow.

### Jeszcze nieosiągnięte

- [x] Programowe rozłożenie ramienia i uruchomienie Scenery WIDE bez aplikacji telefonu (potwierdzone w jednej sesji).
- [ ] Niezawodny, zintegrowany cykl start/stop/park z obsługą błędów i powtórnymi testami po restarcie.
- [ ] Wiarygodny bieżący RAW Scenery.
- [ ] Odtworzenie kolorów producenta z RAW.
- [ ] Stabilna maska nieba/horyzontu.
- [ ] Zweryfikowane wektory ruchu chmur.
- [ ] Kalibrowany pomiar prędkości i prognoza przemieszczania chmur.

**Najważniejszy wynik dnia (aktualizacja):** potwierdzono zarówno kolorowy RTSP WIDE (`rtsp://<IP>:4555/stream`), jak i autonomiczne rozłożenie ramienia, uruchomienie `Scenery` i zapis klatki przez Python bez aplikacji telefonu. Badania RAW i produkcyjna odporność procedury pozostają osobnymi zadaniami.


## 15. AKTUALIZACJA — autonomiczny start WIDE / Scenery bez aplikacji (POTWIERDZONE)

### 15.1. Warunki eksperymentu

- **Model / firmware:** Seestar S30 Pro / `9.31`.
- **System / katalog:** Windows CMD, aktywne `venv-seestarpy`, katalog `C:\Users\treze\OneDrive\Desktop\TSNWorkspace`.
- **Sieć:** Seestar połączony z hotspotem telefonu, IP podczas testu `10.160.154.146`; **adres może się zmienić w kolejnej sesji**. Hotspot był potrzebny jako sieć, ale **nie używano aplikacji Seestar do aktywacji kamer**.
- **Klucz autoryzacyjny:** istniejący lokalny `seestar_alp\firmware-cache\interop.pem`. Nie umieszczać jego treści w repozytorium ani w logach.
- **Bezpieczeństwo:** przed poleceniem rozłożenia upewnić się, że ramię ma wolną przestrzeń. Nie ponawiać polecenia ruchu po timeout bez sprawdzenia fizycznego położenia i stanu `mount`.

### 15.2. Dokładne komendy CMD — sprawdzona kolejność

Wykonać **pojedynczo**, w aktywnym środowisku `venv-seestarpy`, z katalogu `TSNWorkspace`.

**1. Test autoryzowanego połączenia:**

```cmd
python -c "from seestarpy import auth,raw; auth.set_key_path(r'seestar_alp\firmware-cache\interop.pem'); print(raw.test_connection())"
```

**Potwierdzony wynik:** `code: 0`, `result: 'server connected!'`.

**2. Rozłożenie ramienia do pozycji poziomej — komenda wywołuje ruch fizyczny:**

```cmd
python -c "from seestarpy import auth,raw; auth.set_key_path(r'seestar_alp\firmware-cache\interop.pem'); print(raw.scope_move_to_horizon())"
```

**Potwierdzenie obserwacyjne:** użytkownik widział wysuwające się ramię. **Nie ponawiać w trakcie ruchu.**

**3. Potwierdzenie zakończenia ruchu:**

```cmd
python -c "from seestarpy import auth,raw; auth.set_key_path(r'seestar_alp\firmware-cache\interop.pem'); d=raw.get_device_state(); print('MOUNT:',d.get('result',{}).get('mount')); print('CODE:',d.get('code'))"
```

**Potwierdzony wynik:**

```text
MOUNT: {'move_type': 'none', 'close': False, 'tracking': False, 'equ_mode': False}
CODE: 0
```

**4. Uruchomienie `Scenery` z parametrem `cam_id=1`:**

```cmd
python -c "from seestarpy import auth,raw; auth.set_key_path(r'seestar_alp\firmware-cache\interop.pem'); print(raw.send_command({'method':'iscope_start_view','params':{'mode':'scenery','target_ra_dec':[None,None],'target_name':'Unknown','lp_filter':False,'cam_id':1}}))"
```

**Potwierdzony wynik:** `code: 0`, `result: 0`. `target_ra_dec=[None,None]` nie żąda naprowadzania na obiekt. Komenda **może uruchomić oba strumienie**, nie tylko WIDE; potwierdzono to odczytem stanu.

**5. Kontrola rzeczywistego stanu obu kamer:**

```cmd
python -c "from seestarpy import auth,raw; auth.set_key_path(r'seestar_alp\firmware-cache\interop.pem'); d=raw.iscope_get_app_state().get('result',{}); print('SELECTED:',d.get('selected_cam')); print('VIEW:',d.get('View')); print('SECONDVIEW:',d.get('SecondView'))"
```

**Potwierdzony wynik, najważniejsze pola:**

```text
SELECTED: View
VIEW:       mode=scenery, cam_id=0, stage=RTSP, RTSP.state=working, RTSP.port=4554
SECONDVIEW: mode=scenery, cam_id=1, stage=RTSP, RTSP.state=working, RTSP.port=4555
```

Uwaga: `selected_cam` pozostało `View`, mimo że `SecondView` **działało niezależnie**. Nie trzeba przełączać tego pola, aby odbierać obraz WIDE przez RTSP.

**6. Pobranie i zapis kolorowej klatki WIDE:**

```cmd
python -c "import cv2; c=cv2.VideoCapture('rtsp://10.160.154.146:4555/stream',cv2.CAP_FFMPEG); ok,f=c.read(); print('FRAME:',ok,'SHAPE:',f.shape if ok else None); print('SAVED:',cv2.imwrite('S30Lab/wide_autonomous.png',f) if ok else False); c.release()"
```

**Potwierdzony wynik:**

```text
FRAME: True SHAPE: (1920, 1080, 3)
SAVED: True
```

`SHAPE` to kolejno **wysokość, szerokość, kanały** (1920 × 1080 × 3), a nie szerokość × wysokość. OpenCV przechowuje kolorowe klatki w BGR. Plik zapisano lokalnie jako `S30Lab\wide_autonomous.png`; użytkownik potwierdził, że działa i obraz jest prawidłowy.

**7. Opcjonalne otwarcie pliku w Windows:**

```cmd
start "" "S30Lab\wide_autonomous.png"
```

### 15.3. Co nie zadziałało i dlaczego to ważne

- Pierwsza próba `scope_move_to_horizon()` **bez jawnego ustawienia klucza RSA** zakończyła się timeoutem; urządzenie wypowiedziało komunikat o aktualizacji aplikacji, a ramię pozostało złożone. Po ustawieniu klucza komenda zadziałała. **Przyczyna pierwszego timeoutu nie została niezależnie udowodniona**; związek z autoryzacją jest prawdopodobny, lecz nie przesądzony.
- `iscope_start_view` z `mode='star'`, `cam_id=1` zwróciło `code: 0`, ale stan wskazywał `View.stage=ContinuousExposure`, `SecondView.stage=Sleep`, `selected_cam=View`. **Samo `cam_id=1` w trybie star nie wystarczyło do uruchomienia obrazu WIDE.**
- `iscope_start_view` z `mode='scenery'`, `cam_id=1` uruchomiło `RTSP` dla obu kamer; to jest **sprawdzony wariant** dla bieżącego celu WZRD.
- `seestarpy 0.7.1` nie udostępnia `connection.set_target()`. W jednej próbie autodetekcja UDP nie znalazła urządzenia i biblioteka próbowała połączyć się z `10.0.0.1`, mimo że TCP `10.160.154.146:4700` nadal odpowiadał. Późniejsza autodetekcja ponownie zadziałała. **Nie zakładać, że samo otwarcie portu TCP gwarantuje sukces RPC.**
- Biblioteka nie ma gotowej publicznej funkcji `select_camera` w `raw`. `selected_cam=View` nie blokuje działającego RTSP WIDE w Scenery.

### 15.4. Stan walidacji i następne prace

**[POTWIERDZONE W JEDNEJ SESJI]** RSA RPC → rozłożenie ramienia → `Scenery` → `SecondView.RTSP: working` → odczyt i zapis kolorowej klatki przez OpenCV, bez aplikacji mobilnej.

**[JESZCZE NIEPOTWIERDZONE]** Jeden odporny skrypt automatyzujący całość, wykrywanie zmiennego IP, zachowanie po wielu restartach, kontrolowane zatrzymanie strumieni, bezpieczne parkowanie, timeouty/retry, kontrola świeżości obrazu i odzyskiwanie po utracie Wi-Fi.

**Minimalne warunki akceptacji przyszłego skryptu:** potwierdzenie `mount.close=False` i `move_type=none` przed startem obrazu; `SecondView.mode=scenery`, `SecondView.stage=RTSP`, port `4555`; `VideoCapture.read()` zwraca klatkę o oczekiwanym kształcie; zapis pliku się udaje. Nie traktować samego `code:0` jako dowodu aktywnego wideo.

---

*Raport sporządzony na podstawie dzisiejszych logów CMD, wyników eksperymentów i obserwacji użytkownika. Brakujące dane nie zostały uzupełnione fikcyjnymi pomiarami.*
