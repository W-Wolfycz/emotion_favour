(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.EmotionFavourPersonaLogic = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function filterPersonaOptions(options, query) {
    const needle = String(query || '').trim().toLocaleLowerCase();
    return Array.from(options || []).filter(option =>
      !needle || String(option).toLocaleLowerCase().includes(needle)
    );
  }

  function nextPersonaIndex(activeIndex, direction, optionCount) {
    const count = Number(optionCount) || 0;
    if (count <= 0) return -1;
    if (activeIndex < 0) return direction < 0 ? count - 1 : 0;
    return ((activeIndex + direction) % count + count) % count;
  }

  function chooseInitialPersona(personas, currentPersona, storedPersona) {
    const options = Array.from(personas || []);
    if (options.includes(currentPersona)) return currentPersona;
    if (options.includes(storedPersona)) return storedPersona;
    if (options.includes('default')) return 'default';
    return options[0] || '';
  }

  return {
    chooseInitialPersona,
    filterPersonaOptions,
    nextPersonaIndex,
  };
}));
