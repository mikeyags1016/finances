(() => {
  const rows = [...document.querySelectorAll("tbody tr[data-flow-type][data-bucket]")];
  if (!rows.length) return;

  const controls = [...document.querySelectorAll(".pie-slice, .legend-filter")];
  const count = document.getElementById("transaction-count");
  const clearButton = document.getElementById("clear-filter");
  const noResults = document.getElementById("no-filter-results");
  let activeFilter = null;

  const renderFilter = () => {
    let visibleRows = 0;
    for (const row of rows) {
      const matches = !activeFilter
        || (row.dataset.flowType === activeFilter.flowType
          && row.dataset.bucket === activeFilter.bucket);
      row.hidden = !matches;
      if (matches) visibleRows += 1;
    }

    count.textContent = `${visibleRows} shown`;
    clearButton.hidden = !activeFilter;
    noResults.hidden = !activeFilter || visibleRows > 0;

    for (const control of controls) {
      const isActive = activeFilter
        && control.dataset.flowType === activeFilter.flowType
        && control.dataset.bucket === activeFilter.bucket;
      const isDimmed = activeFilter
        && control.dataset.flowType === activeFilter.flowType
        && control.dataset.bucket !== activeFilter.bucket;
      control.classList.toggle("is-active", Boolean(isActive));
      control.classList.toggle("is-dimmed", Boolean(isDimmed));
      if (control.matches(".legend-filter")) {
        control.setAttribute("aria-pressed", String(Boolean(isActive)));
      } else {
        control.setAttribute("aria-pressed", String(Boolean(isActive)));
      }
    }
  };

  const toggleFilter = (control) => {
    const sameFilter = activeFilter
      && activeFilter.flowType === control.dataset.flowType
      && activeFilter.bucket === control.dataset.bucket;
    activeFilter = sameFilter
      ? null
      : { flowType: control.dataset.flowType, bucket: control.dataset.bucket };
    renderFilter();
  };

  document.addEventListener("click", (event) => {
    const control = event.target.closest(".pie-slice, .legend-filter");
    if (control) toggleFilter(control);
  });

  document.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches(".pie-slice")) {
      event.preventDefault();
      toggleFilter(event.target);
    }
  });

  clearButton.addEventListener("click", () => {
    activeFilter = null;
    renderFilter();
  });
})();
