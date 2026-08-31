# STRATEGY — QuotaVerace Pro

> La strategia è l'elemento più importante del progetto. Questo documento
> raccoglie i principi confermati dalla ricerca (2025-2026), come sono
> implementati nel codice e come monitorare se l'edge è reale.

## I due principi matematici (unica strada al profitto)

Solo due strategie hanno un edge matematico dimostrato nel lungo periodo
[fonte: betherosports.com/best-sports-betting-strategy, 2026]:

1. **Value betting (+EV)**: puntare solo quando la quota offerta è SUPERIORE
   alla probabilità vera dell'esito.
2. **Arbitrage (surebet)**: sfruttare differenze di prezzo tra bookmaker per
   profitto garantito.

Tutto il resto (sistemi, trend, "hot picks", martingale) è rumore: nessun
schema di stake può rendere profittevole una scommessa a EV negativo.

QuotaVerace implementa entrambe: il **value filter** (`value_filter.py`) per
il +EV e il **surebet scanner** (`surebet_scanner.py`) per l'arbitraggio.

## Il test decisivo: battere la closing line (CLV)

La closing line (quota di chiusura del mercato) è la stima più accurata
della probabilità reale: incorpora tutte le informazioni disponibili fino
al calcio d'inizio [fonte: sharpfootballanalysis.com/clv-betting, 2026].
I bettor che battono costantemente la closing line sono profittevoli nel
lungo periodo — CLV è il segnale, il record vittorie/sconfitte è rumore.

**Implementazione:**
- `tracker.py` → `clv_history`: confronta la quota del segnale con la quota
  di chiusura del mercato (media CLV mostrata in `/stats`).
- `backtest.py` → split ROI tra segnali che **battono il mercato** (edge
  ≥ +3pp) e quelli che non lo battono.

## Devigging: la probabilità "vera" è quella del mercato privata del margine

I bookmaker gonfiano le quote (margine/vig): la somma delle probabilità
implicite supera il 100%. Il devigging rimuove questo margine per stimare
la probabilità fair [fonte: betherosports.com/devigging-methods-explained,
2026].

**Metodi** (`market_calib.py`):
- `multiplicative`: default semplice, ok su mercati bilanciati;
- `power`: corregge il **favourite-longshot bias** — i book caricano più
  margine sui longshot, quindi la probabilità fair del favorito sale.
  **Default consigliato per 1X2 e Over/Under.**
- `shin`: modello di insider trading di Shin (1993), il più aggressivo sul
  bias; per mercati lopsided a molti esiti.

## Favourite-longshot bias

I longshot (quote alte) sono sopravvalutati dal pubblico: il modello tende a
sovrastimare la loro probabilità. Correzioni applicate:
1. **Devig power/Shin** nei prezzi di mercato (`market_calib.devig`);
2. **`favourite_longshot_adjust`**: sopra quota 3.5 la probabilità del
   modello viene compressa verso il mercato;
3. Il filtro EV_MAX (+15%) scarta le anomalie (probabile errore dati o
   modello sovrastimato).

## Blending modello + mercato

Il mercato è quasi sempre meglio calibrato del modello: mescolare le due
stime riduce l'overconfidence (causa n.1 dei falsi segnali). La probabilità
finale è `0.5 × modello + 0.5 × mercato` (`blend_probability`), e l'EV
viene calcolato su questa probabilità blend.

## Flusso del segnale (fixture_engine._analyze_match)

```
1. LINE SHOPPING      → per ogni esito, il MIGLIOR prezzo tra tutti i
                        bookmaker (l'edge più facile da raccogliere).
2. DEVIG              → prezzi di mercato privati del margine (power):
                        probabilità fair = proxy della closing line.
3. BLEND              → prob finale = 0.5×modello + 0.5×mercato,
                        con correzione longshot sopra quota 3.5.
4. EV                 → calcolato sulla probabilità blend.
5. BEATING THE MARKET → il segnale è valore SOLO se il modello batte il
                        mercato di almeno +3pp (MARKET_EDGE_MIN);
                        strong_value richiede EV>8% e edge ≥ +5pp.
```

## Gestione del bankroll (Kelly frazionario)

- Singole: **1/4 Kelly**, cap 3% del bankroll (`kelly_euro`).
- Multiple: **1/8 Kelly**, cap 1% (`multipla_stake`) — la multipla
  concentra tutto il rischio in una sola scommessa.
- Mai più del 5% su una singola scommessa, qualunque sia l'edge.
- Il Kelly frazionario è lo standard dei professionisti: crescita ~50%
  di quella del Kelly pieno con metà della volatilità.

## Volume e calibrazione

- Servono **500-1.000+ scommesse chiuse** perché l'edge emerga dal rumore.
- Il backtest (`/backtest`) confronta EV atteso (modello) vs ROI realizzato:
  - ROI ≈ EV → modello calibrato, edge reale;
  - ROI molto < EV su campione ampio → modello ottimista, non puntare.
- Da 100 scommesse chiuse il CLV medio è il primo segnale affidabile.

## Indicatori di mercato da monitorare

| Metrica | Dove | Cosa indica |
|---|---|---|
| CLV medio | `/stats` | Stai battendo la closing line? |
| ROI vs EV (gap) | `/backtest` | Il modello è calibrato o ottimista? |
| ROI "batte mercato" vs "non batte" | `/backtest` | Conferma del test CLV |
| Edge sul mercato per segnale | `/schedina`, `/value` | Quanto batti il consenso |

## Fonti

- Bet Hero — *Most Profitable Sports Betting Strategies (2026)*
- Bet Hero — *Devigging Methods: Power, Shin, Additive, Multiplicative (2026)*
- XCLSV Media — *Closing Line Value (CLV) Explained (2026)*
- Sharp Football Analysis — *What is CLV Betting?*
- Shin (1993) — modello di insider trading per il margine dei bookmaker
- Dixon & Coles (1997) — correzione della correlazione dei pareggi bassi
