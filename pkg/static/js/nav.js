document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-nav-toggle]').forEach((toggle) => {
    const target = document.getElementById(toggle.dataset.navTarget);
    if (!target) return;

    const closeMenu = () => {
      target.classList.remove('is-open');
      toggle.setAttribute('aria-expanded', 'false');
    };

    toggle.addEventListener('click', () => {
      const isOpen = target.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(isOpen));
    });

    target.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', closeMenu);
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeMenu();
    });
  });
});
