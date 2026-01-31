(function () {
  const subtitleEl = document.getElementById('subtitle');
  const descriptionEl = document.getElementById('description');
  const selectEl = document.getElementById('language-select');

  if (!subtitleEl || !descriptionEl || !selectEl) {
    return;
  }

  let locales = {};

  function applyLanguage(lang) {
    const entry = locales[lang] || locales['en'];
    if (!entry) {
      return;
    }

    subtitleEl.textContent = entry.subtitle;
    descriptionEl.textContent = entry.body;
  }

  fetch('/assets/data/landing-locales.json')
    .then(function (response) { return response.json(); })
    .then(function (data) {
      locales = data || {};
      const initial = selectEl.value || 'en';
      applyLanguage(initial);
    })
    .catch(function (error) {
      console.error('Failed to load locales for Knitter landing page.', error);
    });

  selectEl.addEventListener('change', function () {
    applyLanguage(selectEl.value);
  });
})();
