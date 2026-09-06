// Progresywne wzbogacenie. Strona działa bez JS-a — to tylko warstwa
// potwierdzeń, odświeżania postępu i filtrowania po stronie klienta.
(function () {
  "use strict";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // -----------------------------------------------------------------------
  // Potwierdzenie kliknięcia < 100 ms.
  // Formularze robią pełny POST + redirect; bez tego przycisk wygląda na
  // martwy przez cały przelot do serwera, a użytkownik klika drugi raz.
  // -----------------------------------------------------------------------
  document.querySelectorAll("form[data-busy-form]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      const btn = e.submitter || form.querySelector("button");
      if (!btn || btn.dataset.busy === "1") return;
      // Zapamiętaj który przycisk kliknięto — `disabled` wycina go z payloadu,
      // więc wartość `name=kind` wędruje do ukrytego pola.
      if (btn.name) {
        const carry = document.createElement("input");
        carry.type = "hidden";
        carry.name = btn.name;
        carry.value = btn.value;
        form.appendChild(carry);
      }
      btn.dataset.busy = "1";
      btn.setAttribute("aria-busy", "true");
      const label = btn.dataset.busyLabel;
      if (label) {
        btn.dataset.idleLabel = btn.textContent.trim();
        btn.textContent = label;
      }
      // Gdyby nawigacja nie doszła do skutku (błąd sieci), oddaj przycisk.
      setTimeout(() => {
        btn.dataset.busy = "";
        btn.removeAttribute("aria-busy");
        if (btn.dataset.idleLabel) btn.textContent = btn.dataset.idleLabel;
      }, 12000);
    });
  });

  // -----------------------------------------------------------------------
  // Potwierdzenie akcji, która kosztuje (ponowne ASR).
  //
  // Przechwytujemy `click`, nie `submit` — dzięki temu anulowanie w ogóle nie
  // odpala handlera wyżej i przycisk nie zostaje na „Queueing…”. Potwierdzenie
  // wraca przez `requestSubmit(btn)`, żeby `e.submitter` (a więc `name=kind`)
  // był ten sam co przy zwykłym kliknięciu. requestSubmit nie klika przycisku,
  // więc pętli nie ma.
  //
  // Decyzję czytamy z kliknięcia w przycisk OK, a nie ze zdarzenia `close`
  // i `dialog.returnValue`. Wersja przez `close` wygląda czyściej, ale
  // sprawdzona w Chrome nie zadziałała: dialog zamykał się z poprawnym
  // returnValue, a listener `close` nie dostawał zdarzenia — czyli klik
  // w „potwierdź" cicho nie robił nic. Anulowanie i Esc zostają natywne.
  //
  // Zakłada jeden przycisk-wyzwalacz na dialog (stąd listener na OK rejestrowany
  // raz, nie przy każdym otwarciu — inaczej po anuluj→otwórz submit poszedłby
  // dwa razy).
  //
  // Bez <dialog> (albo bez JS-a) akcja idzie bez pytania. Świadomie: to
  // narzędzie wewnętrzne za whitelistą domen, więc lepiej działający przycisk
  // niż zablokowany.
  // -----------------------------------------------------------------------
  document.querySelectorAll("[data-confirm]").forEach((btn) => {
    const dialog = document.getElementById(btn.dataset.confirm);
    const ok = dialog && dialog.querySelector("[data-confirm-ok]");
    if (!dialog || !ok || typeof dialog.showModal !== "function") return;
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      dialog.showModal();
    });
    ok.addEventListener("click", () => {
      dialog.close();
      btn.form.requestSubmit(btn);
    });
  });

  // -----------------------------------------------------------------------
  // Process automatically: zapis przy przełączeniu, bez POST-a Sync.
  // Bez JS-a checkbox zostaje polem formularza Sync — stary fallback.
  // -----------------------------------------------------------------------
  document.querySelectorAll("[data-autoprocess]").forEach((box) => {
    let inflight = false;
    let confirmed = box.checked;
    const persist = async () => {
      if (inflight) return;
      inflight = true;
      box.setAttribute("aria-busy", "true");
      try {
        while (true) {
          const intended = box.checked;
          box.removeAttribute("aria-invalid");
          const body = new URLSearchParams();
          if (intended) body.set("autoprocess", "true");
          try {
            const res = await fetch("/api/settings/autoprocess", {
              method: "POST",
              headers: {
                Accept: "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
              },
              body,
            });
            if (!res.ok) throw new Error("save failed");
          } catch (_) {
            if (box.checked === intended) {
              box.checked = confirmed;
              box.setAttribute("aria-invalid", "true");
              return;
            }
            continue;
          }
          confirmed = intended;
          if (box.checked === intended) return;
        }
      } finally {
        inflight = false;
        box.removeAttribute("aria-busy");
      }
    };
    box.addEventListener("change", persist);
  });

  // -----------------------------------------------------------------------
  // Filtry: submit po zmianie kontrolki, debounce na szukajce.
  // -----------------------------------------------------------------------
  const form = document.querySelector("form[data-autosubmit]");
  if (form) {
    form.addEventListener("change", (e) => {
      if (e.target.matches("input[type=checkbox], select, input[type=date]")) {
        form.querySelector("input[name=page]")?.remove();
        form.submit();
      }
    });
    const q = form.querySelector("input[name=q]");
    if (q) {
      let t;
      q.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => form.submit(), 450);
      });
    }
  }

  // -----------------------------------------------------------------------
  // Zawężanie długiej listy w filtrach (osoby). Tylko po stronie klienta —
  // zaznaczone pozycje zostają zaznaczone, nawet gdy je odfiltrujemy z widoku.
  // -----------------------------------------------------------------------
  document.querySelectorAll("[data-list-filter]").forEach((input) => {
    const list = document.querySelector(input.dataset.listFilter);
    if (!list) return;
    const rows = Array.from(list.querySelectorAll("[data-filter-text]"));
    const emptyBox = list.querySelector("[data-filter-empty]");
    input.addEventListener("input", () => {
      const needle = input.value.trim().toLowerCase();
      let shown = 0;
      rows.forEach((row) => {
        // Zaznaczonych nie chowamy — użytkownik musi widzieć, co ma włączone.
        const checked = row.querySelector("input")?.checked;
        const match = !needle || checked || row.dataset.filterText.includes(needle);
        row.hidden = !match;
        if (match) shown++;
      });
      if (emptyBox) emptyBox.hidden = shown !== 0;
    });
  });

  // -----------------------------------------------------------------------
  // Postęp zadań na żywo.
  // Pollujemy tylko zadania aktywne przy renderze — inaczej strona z samymi
  // zakończonymi jobami wpadłaby w pętlę przeładowań.
  // -----------------------------------------------------------------------
  const watched = Array.from(
    document.querySelectorAll("[data-job-id][data-job-active]")
  ).map((el) => el.dataset.jobId);

  function paint(job) {
    document.querySelectorAll(`[data-job-id="${job.id}"]`).forEach((el) => {
      const bar = el.querySelector(".bar");
      if (bar) {
        bar.querySelector("i").style.setProperty("--p", job.progress / 100);
        bar.setAttribute("aria-valuenow", job.progress);
        bar.classList.toggle("err", job.status === "failed");
      }
      const step = el.querySelector("[data-job-step]");
      if (step) step.textContent = job.step || job.status_label || job.status;
      const badge = el.querySelector("[data-job-status]");
      if (badge) {
        badge.textContent = job.status_label || job.status;
        badge.className = "badge b-" + job.status;
      }
    });
  }

  let stopped = false;
  async function tick() {
    if (stopped) return;
    try {
      const res = await fetch("/api/jobs?ids=" + watched.join(","), {
        headers: { Accept: "application/json" },
      });
      if (res.status === 401) {
        stopped = true;
        return;
      }
      const data = await res.json();
      let anyActive = false;
      data.items.forEach((job) => {
        paint(job);
        if (job.status === "queued" || job.status === "running") anyActive = true;
      });
      // Wszystko, co śledziliśmy, doszło do końca → przeładuj, żeby pokazać
      // wynik (transkrypt, błąd, nowe zadania).
      if (!anyActive && data.items.length) {
        stopped = true;
        setTimeout(() => location.reload(), 800);
      }
    } catch (_) {
      /* offline — spróbujemy przy następnym ticku */
    }
  }
  if (watched.length) {
    tick();
    setInterval(tick, 2500);
  }

  // --- log joba na żywo ---
  const logBox = document.querySelector("[data-job-log]");
  if (logBox) {
    const id = logBox.dataset.jobLog;
    const pull = async () => {
      try {
        const res = await fetch(`/api/jobs/${id}/log`);
        const txt = await res.text();
        if (txt !== logBox.textContent) {
          const atBottom =
            logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 30;
          logBox.textContent = txt;
          if (atBottom) logBox.scrollTop = logBox.scrollHeight;
        }
      } catch (_) {}
    };
    pull();
    setInterval(pull, 3000);
  }

  // -----------------------------------------------------------------------
  // Transkrypt: szukanie w treści + filtr po mówcy (jedno wspólne przejście).
  // -----------------------------------------------------------------------
  const tSearch = document.querySelector("[data-transcript-search]");
  const spSel = document.querySelector("[data-speaker-filter]");
  if (tSearch || spSel) {
    const utts = Array.from(document.querySelectorAll(".utt"));
    const counter = document.querySelector("[data-transcript-count]");
    const emptyBox = document.querySelector("[data-search-empty]");

    const plural = (n) => (n === 1 ? "1 match" : `${n} matches`);

    const refresh = () => {
      const needle = (tSearch?.value || "").trim().toLowerCase();
      const speaker = spSel?.value || "";
      let shown = 0;
      let hits = 0;
      utts.forEach((u) => {
        const okText = !needle || u.dataset.text.includes(needle);
        const okSpeaker = !speaker || u.dataset.speaker === speaker;
        const visible = okText && okSpeaker;
        u.hidden = !visible;
        u.classList.toggle("hit", Boolean(needle) && visible);
        if (visible) {
          shown++;
          if (needle) hits++;
        }
      });
      if (counter) counter.textContent = needle ? plural(hits) : "";
      if (emptyBox) emptyBox.hidden = shown !== 0;
    };

    tSearch?.addEventListener("input", refresh);
    spSel?.addEventListener("change", refresh);
  }

  // -----------------------------------------------------------------------
  // Zaznaczanie wierszy do akcji masowych.
  // -----------------------------------------------------------------------
  const all = document.querySelector("[data-check-all]");
  if (all) {
    all.addEventListener("change", () => {
      document.querySelectorAll("input[name=meeting_ids]").forEach((c) => {
        c.checked = all.checked;
      });
      syncBulk();
    });
  }
  function syncBulk() {
    const n = document.querySelectorAll("input[name=meeting_ids]:checked").length;
    const bar = document.querySelector("[data-bulk]");
    if (!bar) return;
    bar.hidden = n === 0;
    const label = bar.querySelector("[data-bulk-count]");
    if (label) label.textContent = n;
  }
  document.addEventListener("change", (e) => {
    if (e.target.name === "meeting_ids") syncBulk();
  });
  syncBulk();

  // -----------------------------------------------------------------------
  // Kopiowanie transkryptu — jedno ciche potwierdzenie, bez toastów.
  // -----------------------------------------------------------------------
  document.querySelector("[data-copy]")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const src = document.querySelector(btn.dataset.copy);
    if (!src) return;
    const idle = btn.dataset.idle || btn.textContent.trim();
    btn.dataset.idle = idle;
    try {
      await navigator.clipboard.writeText(src.innerText);
      btn.textContent = "Copied";
    } catch (_) {
      btn.textContent = "Copy failed";
    }
    if (!reduced) btn.style.transition = "none";
    setTimeout(() => {
      btn.textContent = idle;
      btn.style.transition = "";
    }, 1600);
  });
})();
