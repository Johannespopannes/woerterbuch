/* wb.js — gemeinsame Wörterbuch-Funktionen (siehe FUNKTIONSKATALOG.md).
   Jede Funktion genau einmal; Seiten rufen sie auf und passen nur die
   Darstellung an, nicht Texte oder Bewertungsregeln. Kein Build-Schritt. */
(function () {
  'use strict';
  const WB = window.WB = {};

  // ---------- Sprach-Metadaten ----------
  const NAMEN = { de: 'Deutsch', en: 'Englisch', fr: 'Französisch',
                  es: 'Spanisch', it: 'Italienisch',
                  ar: 'Arabisch', tn: 'Tunesisch', tlh: 'Klingonisch',
                  ru: 'Russisch', zh: 'Chinesisch', ja: 'Japanisch',
                  hi: 'Hindi' };
  // ADJ endet auf '-' → im Satz direkt an 'Wort' geheftet (Hindi-Wort)
  const ADJ = { de: 'deutsches', en: 'englisches', fr: 'französisches',
                es: 'spanisches', it: 'italienisches',
                ar: 'arabisches', tn: 'tunesisches', tlh: 'klingonisches',
                ru: 'russisches', zh: 'chinesisches', ja: 'japanisches',
                hi: 'Hindi-' };
  WB.sprachName = (l) => NAMEN[l] || l;
  WB.sprachAdj = (l) => ADJ[l] || (NAMEN[l] || l);

  // ---------- Kleine Helfer ----------
  WB.show = (el, on) => { el.style.display = on ? '' : 'none'; };

  // ---------- Flexion (dauerhaft sichtbar, mobil transponiert) ----------
  const SCHMAL = window.matchMedia('(max-width: 620px)');
  let letzteGrammatik = null;

  // Bei Groessenwechsel die letzte Grammatik-Karte neu rendern (Transposition)
  SCHMAL.addEventListener('change', () => {
    if (letzteGrammatik) WB.grammatikKarte(
      letzteGrammatik.els, letzteGrammatik.grammar,
      letzteGrammatik.wort, letzteGrammatik.lang);
  });

  function flexTabelle(zeilen, transponiert) {
    const vals = Object.values(zeilen);
    const istMatrix = vals.some(v => v && typeof v === 'object');
    const tab = document.createElement('table');
    if (!istMatrix) {
      for (const [k, v] of Object.entries(zeilen)) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.textContent = k;
        const td = document.createElement('td');
        td.textContent = v;
        tr.append(th, td);
        tab.appendChild(tr);
      }
      return tab;
    }
    const reihen = Object.entries(zeilen);        // z. B. Singular/Plural
    const spalten = [...new Set(vals.filter(v => v && typeof v === 'object')
      .flatMap(v => Object.keys(v)))];
    const trh = document.createElement('tr');
    trh.appendChild(document.createElement('th'));
    const kopf = transponiert ? reihen.map(r => r[0]) : spalten;
    for (const s of kopf) {
      const th = document.createElement('th');
      th.textContent = s;
      trh.appendChild(th);
    }
    tab.appendChild(trh);
    const zeilenListe = transponiert
      ? spalten.map(s => [s, Object.fromEntries(reihen.map(([n, zv]) => [n, zv && zv[s]]))])
      : reihen;
    for (const [name, zv] of zeilenListe) {
      const tr = document.createElement('tr');
      const th = document.createElement('th');
      th.textContent = name;
      tr.appendChild(th);
      for (const s of (transponiert ? reihen.map(r => r[0]) : spalten)) {
        const td = document.createElement('td');
        td.textContent = (zv && zv[s]) || '—';
        tr.appendChild(td);
      }
      tab.appendChild(tr);
    }
    return tab;
  }

  // Rendert alle Blöcke (cases/tenses/comparison) ohne interne Feldnamen.
  WB.renderFlexion = function (box, flexion) {
    box.innerHTML = '';
    if (!flexion || typeof flexion !== 'object') return false;
    const bloecke = Object.values(flexion)
      .filter(v => v && typeof v === 'object');
    if (!bloecke.length) return false;
    for (const zeilen of bloecke) {
      box.appendChild(flexTabelle(zeilen, SCHMAL.matches));
    }
    return true;
  };

  // Baut Zeile + Flexion in eine Grammatik-Karte.
  // els: {zeile, flexwrap, flexbox}; grammar: {word_class, gender, article, lemma, flexion}
  WB.grammatikKarte = function (els, grammar, wort, lang) {
    if (!grammar) { WB.show(els.flexwrap, false); return; }
    letzteGrammatik = { els, grammar, wort, lang };
    const teile = [];
    if (grammar.article) teile.push(grammar.article);
    if (grammar.gender) {
      teile.push({ m: 'maskulin', f: 'feminin', n: 'neutrum' }[grammar.gender]
                 || grammar.gender);
    }
    if (grammar.word_class) teile.push(grammar.word_class);
    els.zeile.textContent = teile.join(' · ');
    if (grammar.lemma && grammar.lemma !== wort) {
      if (teile.length) els.zeile.appendChild(document.createTextNode(' · Grundform: '));
      else els.zeile.appendChild(document.createTextNode('Grundform: '));
      const a = document.createElement('a');
      a.href = '/worteintrag?' + new URLSearchParams({ word: grammar.lemma, lang });
      a.textContent = grammar.lemma;
      els.zeile.appendChild(a);
    }
    const hatFlex = WB.renderFlexion(els.flexbox, grammar.flexion);
    WB.show(els.flexwrap, hatFlex);
  };

  // ---------- Wort-Bewertung (drei Fälle) ----------
  WB.wortVote = async function (entryId, kind) {
    const r = await fetch('/wort/vote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entry_id: entryId, kind }),
    });
    return r.ok;
  };

  // ---------- Bild-Bewertung (eine Implementierung, zwei Flüsse) ----------
  // opts: {entryId, understood, image?, nonce?, kind?, session}
  //   session: 'wortseite' -> POST /wort/bildvote (Sitzungs-Modell)
  //            'bewerten'  -> POST /raten/vote (Einmal-Nonce-Modell)
  // liefert {ok, status}
  WB.bildVote = async function (opts) {
    let url, payload;
    if (opts.session === 'bewerten') {
      url = '/raten/vote';
      payload = { entry_id: opts.entryId, understood: opts.understood,
                  image: opts.image || null, kind: opts.kind || 'image',
                  nonce: opts.nonce };
    } else {
      url = '/wort/bildvote';
      payload = { entry_id: opts.entryId, understood: opts.understood,
                  image: opts.image || null };
    }
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return { ok: r.ok || r.status === 409, status: r.status };
  };

  // Verbindliche Bildfrage und Antworten — ein Ort für alle Seiten.
  WB.BILD_FRAGEN = {
    frage: 'Versteht man das Bild?',
    ja: 'Ja',
    nein: 'Nein',
  };
})();
