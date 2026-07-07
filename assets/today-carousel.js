// Carrusel "Partidos de hoy" — compartido por todas las páginas.
// Requiere en el HTML:
//   <section class="today-section" id="todaySection" style="display:none;">
//     <h2>Partidos de hoy</h2>
//     <div class="carousel" id="todayCarousel"></div>
//   </section>
(function () {
  const FIXTURES_URL = "data/fixtures.json";
  const TEAMS_URL = "data/teams.json";
  const MATCHES_URL = "data/matches.json";
  const REFRESH_MS = 30000;  // refresco cada 30s (antes 60s) para bajar el lag percibido
  const LIVE_STATUSES = ["IN_PLAY", "PAUSED", "LIVE"];
  const FINISHED_STATUSES = ["FINISHED"];

  // Heuristica para distinguir el "limbo" post-partido: football-data marca el
  // partido como PAUSED durante revisiones VAR / cierre antes de FINISHED. Si
  // estamos en PAUSED y han pasado ya >= 100 min desde el kickoff, lo tratamos
  // como "Finalizando" en lugar de "En directo".
  function isProbablyEnding(f) {
    if (f.status !== "PAUSED") return false;
    // Si el minuto del partido es >= 90, casi seguro es cierre de partido
    if (typeof f.minute === "number" && f.minute >= 90) return true;
    // Fallback temporal: si pasaron mas de 100 min desde el kickoff
    if (f.utcDate) {
      const startMs = new Date(f.utcDate).getTime();
      if (!isNaN(startMs) && Date.now() - startMs > 100 * 60 * 1000) return true;
    }
    return false;
  }
  // Pausa que SI es de descanso real (no de fin)
  function isHalftimeBreak(f) {
    if (f.status !== "PAUSED") return false;
    return !isProbablyEnding(f);
  }
  const GROUP_LABELS = { GROUP_A: "Grupo A", GROUP_B: "Grupo B", GROUP_C: "Grupo C", GROUP_D: "Grupo D", GROUP_E: "Grupo E", GROUP_F: "Grupo F", GROUP_G: "Grupo G", GROUP_H: "Grupo H" };
  const STAGE_LABELS = {
    GROUP_STAGE: "Fase de grupos",
    LAST_32: "Dieciseisavos",
    ROUND_OF_32: "Dieciseisavos",
    LAST_16: "Octavos de final",
    ROUND_OF_16: "Octavos de final",
    QUARTER_FINALS: "Cuartos de final",
    SEMI_FINALS: "Semifinales",
    FINAL: "Final",
  };

  let TEAM_NAMES = {};

  function escapeHtml(s) { return (s ?? "").toString().replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])); }
  function teamName(fdName) { return TEAM_NAMES[(fdName || "").toLowerCase()] || fdName || "?"; }
  function stageGroupLabel(f) { return f.group ? GROUP_LABELS[f.group] || f.group : (STAGE_LABELS[f.stage] || f.stage || ""); }

  function crestImg(url, alt) {
    if (!url) return "";
    return `<img class="mc-crest" src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" onerror="this.remove()">`;
  }

  function renderMatchCard(f) {
    const ending = isProbablyEnding(f);
    const halftime = isHalftimeBreak(f);
    const isLive = LIVE_STATUSES.includes(f.status) && !ending;
    const isFinished = FINISHED_STATUSES.includes(f.status);
    const home = teamName(f.home_team);
    const away = teamName(f.away_team);
    const showScore = LIVE_STATUSES.includes(f.status) || isFinished;
    const homeScore = showScore ? (f.home_goals ?? 0) : "";
    const awayScore = showScore ? (f.away_goals ?? 0) : "";
    let footer;
    if (ending) {
      footer = `<div class="mc-time" title="Pendiente del FINISHED oficial">Finalizando…</div>`;
    } else if (isLive) {
      const label = halftime ? "Descanso" : (f.minute ? f.minute + "'" : "En directo");
      footer = `<span class="mc-badge-live">${label}</span>`;
    } else if (isFinished) {
      footer = `<div class="mc-time">Finalizado</div>`;
    } else {
      const time = new Date(f.utcDate).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
      footer = `<div class="mc-time">${time}</div>`;
    }
    return `
      <div class="match-card${isLive ? " live" : ""}">
        <div class="mc-group">${escapeHtml(stageGroupLabel(f))}</div>
        <div class="mc-team">${crestImg(f.home_crest, home)}<span class="mc-name">${escapeHtml(home)}</span><span class="mc-score">${homeScore}</span></div>
        <div class="mc-team">${crestImg(f.away_crest, away)}<span class="mc-name">${escapeHtml(away)}</span><span class="mc-score">${awayScore}</span></div>
        ${footer}
      </div>
    `;
  }

  async function loadTodayMatches() {
    const section = document.getElementById("todaySection");
    const carousel = document.getElementById("todayCarousel");
    if (!section || !carousel) return;

    try {
      const [fixturesDoc, teamsDoc, matchesDoc] = await Promise.all([
        fetch(FIXTURES_URL + "?t=" + Date.now()).then(r => r.json()),
        fetch(TEAMS_URL + "?t=" + Date.now()).then(r => r.json()).catch(() => null),
        fetch(MATCHES_URL + "?t=" + Date.now()).then(r => r.json()).catch(() => null),
      ]);

      const scoredIds = new Set((matchesDoc && matchesDoc.matches || []).map(m => m.id));

      TEAM_NAMES = {};
      if (teamsDoc && teamsDoc.teams) {
        for (const t of teamsDoc.teams) {
          if (t.fd_name) TEAM_NAMES[t.fd_name.toLowerCase()] = t.name;
        }
      }

      const now = new Date();
      const todayFixtures = (fixturesDoc.fixtures || []).filter(f => {
        if (scoredIds.has(f.id) && !FINISHED_STATUSES.includes(f.status)) f.status = "FINISHED";
        return new Date(f.utcDate).toDateString() === now.toDateString()
          || LIVE_STATUSES.includes(f.status);
      });

      if (todayFixtures.length === 0) {
        section.style.display = "none";
        return;
      }

      todayFixtures.sort((a, b) => {
        const aLive = LIVE_STATUSES.includes(a.status);
        const bLive = LIVE_STATUSES.includes(b.status);
        if (aLive !== bLive) return aLive ? -1 : 1;
        return new Date(a.utcDate) - new Date(b.utcDate);
      });

      const heading = section.querySelector("h2");
      if (heading) {
        const hasLive = todayFixtures.some(f => LIVE_STATUSES.includes(f.status));
        heading.innerHTML = "Partidos de hoy" + (hasLive ? ` <span class="today-live-badge">🔴 En directo</span>` : "");
      }

      section.style.display = "";
      carousel.innerHTML = todayFixtures.map(renderMatchCard).join("");
    } catch {
      section.style.display = "none";
    }
  }

  loadTodayMatches();
  setInterval(loadTodayMatches, REFRESH_MS);
})();
