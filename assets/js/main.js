/* =========================================================================
   WayinVideo — interactions
   ========================================================================= */
(function () {
  'use strict';

  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let cycle = 'monthly'; // shared billing state

  /* ---------------------------------------------------------------- Promo */
  const promoClose = $('#promoClose');
  if (promoClose) {
    promoClose.addEventListener('click', () => $('#promoBar').classList.add('hidden'));
  }

  /* --------------------------------------------------------------- Header */
  const header = $('#siteHeader');
  const toTop = $('#toTop');

  const onScroll = () => {
    const y = window.scrollY;
    if (header) header.classList.toggle('scrolled', y > 12);
    if (toTop) toTop.classList.toggle('show', y > 600);
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  if (toTop) toTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' }));

  /* ------------------------------------------------------- Mobile burger */
  const burger = $('#burger');
  const mainNav = $('#mainNav');
  if (burger && mainNav) {      burger.addEventListener('click', () => {
        const open = mainNav.classList.toggle('open');
        if (open && header) mainNav.style.top = header.getBoundingClientRect().bottom + 'px';
      burger.classList.toggle('active', open);
      burger.setAttribute('aria-expanded', String(open));
    });
    // close the drawer after tapping an anchor link
    $$('#mainNav a[href*="#"]').forEach((a) =>
      a.addEventListener('click', () => {
        mainNav.classList.remove('open');
        burger.classList.remove('active');
        burger.setAttribute('aria-expanded', 'false');
      })
    );
  }

  /* ------------------------------------------------------------ Dropdowns */
  const isDesktop = () => window.matchMedia('(min-width: 901px)').matches;
  const navItems = $$('.nav-item.has-drop');
  let hoverCloseTimer = null;   // grace period so the menu never "blinks" mid-travel

  const closeAll = (except) => {
    navItems.forEach((item) => {
      if (item === except) return;
      item.classList.remove('open');
      const btn = $('.nav-link', item);
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  };

  navItems.forEach((item) => {
    const btn = $('.nav-link', item);
    if (!btn) return;

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (isDesktop()) {
        // hovering already handles it; a click just keeps it open
        closeAll(item);
        item.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
        return;
      }
      const open = !item.classList.contains('open');
      closeAll(item);
      item.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', String(open));
    });

    item.addEventListener('mouseenter', () => {
      if (!isDesktop()) return;
      clearTimeout(hoverCloseTimer);
      closeAll(item);
      item.classList.add('open');
      btn.setAttribute('aria-expanded', 'true');
    });
    item.addEventListener('mouseleave', () => {
      if (!isDesktop()) return;
      // delay the close: crossing the gap/animation must not dismiss the menu
      clearTimeout(hoverCloseTimer);
      hoverCloseTimer = setTimeout(() => {
        item.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
      }, 220);
    });
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.nav-item.has-drop')) closeAll(null);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAll(null);
  });

  /* -------------------------------------------------------- Hero rotator */
  const rotatorHost = $('#heroRotator');
  if (rotatorHost) {
    const phrases = [
      'Understand hours of video instantly',
      'Find any moment in seconds',
      'Turn long videos into viral clips',
      'Transcribe, caption and translate 100+ languages'
    ];
    const parent = rotatorHost.parentNode;
    let i = 0;
    setInterval(() => {
      i = (i + 1) % phrases.length;
      const span = document.createElement('span');
      span.className = 'rotator';
      span.textContent = phrases[i];
      const current = $('.rotator', parent);
      if (current) current.replaceWith(span);
      else parent.appendChild(span);
    }, 2600);
  }

  /* --------------------------------------------------------- Reveal on scroll */
  const revealEls = $$('.reveal');
  if (revealEls.length) {
    if (reduceMotion || !('IntersectionObserver' in window)) {
      revealEls.forEach((el) => el.classList.add('in'));
    } else {
      const io = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add('in');
              io.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.14, rootMargin: '0px 0px -60px' }
      );
      revealEls.forEach((el) => io.observe(el));
    }
  }

  /* ------------------------------------------------------------- Counters */
  const counters = $$('.counter');
  if (counters.length) {
    const animate = (el) => {
      const to = parseFloat(el.dataset.to || '0');
      const suffix = el.dataset.suffix || '';
      const dur = 1500;
      const start = performance.now();
      const step = (now) => {
        const p = Math.min((now - start) / dur, 1);
        const eased = 1 - Math.pow(1 - p, 3);
        const val = Math.round(to * eased);
        el.textContent = val.toLocaleString() + suffix;
        if (p < 1) requestAnimationFrame(step);
      };
      if (reduceMotion) {
        el.textContent = to.toLocaleString() + suffix;
      } else {
        requestAnimationFrame(step);
      }
    };

    if ('IntersectionObserver' in window) {
      const cio = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              animate(entry.target);
              cio.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.5 }
      );
      counters.forEach((c) => cio.observe(c));
    } else {
      counters.forEach(animate);
    }
  }

  /* ---------------------------------------------------------- Feature glow */
  $$('.feat-card').forEach((card) => {
    card.addEventListener('mousemove', (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty('--mx', ((e.clientX - r.left) / r.width) * 100 + '%');
      card.style.setProperty('--my', ((e.clientY - r.top) / r.height) * 100 + '%');
    });
  });

  /* ------------------------------------------------------------------ Tabs */
  const tabs = $$('#solutionTabs .tab');
  if (tabs.length) {
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        $$('#solutionTabs .tab-panel').forEach((p) =>
          p.classList.toggle('active', p.dataset.panel === tab.dataset.tab)
        );
      });
    });
  }

  /* --------------------------------------------------------- Testimonials */
  const slider = $('#testimonialSlider');
  if (slider) {
    const viewport = $('.slider-viewport', slider);
    const track = $('#sliderTrack', slider);
    const cards = $$('.quote-card', track);
    const dotsWrap = $('#sliderDots', slider);
    let index = 0;
    let timer = null;

    const perView = () => (window.innerWidth <= 720 ? 1 : window.innerWidth <= 1080 ? 2 : 3);
    const maxIndex = () => Math.max(0, cards.length - perView());

    const buildDots = () => {
      dotsWrap.innerHTML = '';
      for (let i = 0; i <= maxIndex(); i++) {
        const b = document.createElement('button');
        b.type = 'button';
        b.setAttribute('aria-label', 'Go to testimonial group ' + (i + 1));
        b.addEventListener('click', () => go(i));
        dotsWrap.appendChild(b);
      }
    };

    const render = () => {
      const card = cards[index];
      if (!card) return;
      const x = card.getBoundingClientRect().left - track.getBoundingClientRect().left;
      track.style.transform = `translateX(${-x}px)`;
      $$('button', dotsWrap).forEach((d, i) => d.classList.toggle('active', i === index));
    };

    const go = (i) => {
      index = Math.min(Math.max(i, 0), maxIndex());
      render();
    };

    $('#slideNext').addEventListener('click', () => go(index >= maxIndex() ? 0 : index + 1));
    $('#slidePrev').addEventListener('click', () => go(index <= 0 ? maxIndex() : index - 1));

    const start = () => {
      if (reduceMotion) return;
      stop();
      timer = setInterval(() => go(index >= maxIndex() ? 0 : index + 1), 5000);
    };
    const stop = () => timer && clearInterval(timer);

    slider.addEventListener('mouseenter', stop);
    slider.addEventListener('mouseleave', start);

    // touch / drag support
    let startX = 0;
    viewport.addEventListener('touchstart', (e) => { startX = e.touches[0].clientX; stop(); }, { passive: true });
    viewport.addEventListener('touchend', (e) => {
      const dx = e.changedTouches[0].clientX - startX;
      if (Math.abs(dx) > 45) go(dx < 0 ? index + 1 : index - 1);
      start();
    });

    let resizeT;
    window.addEventListener('resize', () => {
      clearTimeout(resizeT);
      resizeT = setTimeout(() => {
        if (index > maxIndex()) index = maxIndex();
        buildDots();
        render();
      }, 150);
    });

    buildDots();
    render();
    start();
  }

  /* ------------------------------------------------------------ Accordion */
  $$('[data-accordion]').forEach((group) => {
    const items = $$('.acc-item', group);
    items.forEach((item) => {
      const q = $('.acc-q', item);
      const a = $('.acc-a', item);
      if (!q || !a) return;

      q.addEventListener('click', () => {
        const isOpen = item.classList.contains('open');
        items.forEach((other) => {
          other.classList.remove('open');
          const oq = $('.acc-q', other);
          const oa = $('.acc-a', other);
          if (oq) oq.setAttribute('aria-expanded', 'false');
          if (oa) oa.style.maxHeight = null;
        });
        if (!isOpen) {
          item.classList.add('open');
          q.setAttribute('aria-expanded', 'true');
          a.style.maxHeight = a.scrollHeight + 'px';
        }
      });
    });
  });

  window.addEventListener('resize', () => {
    const open = $('.acc-item.open .acc-a');
    if (open) open.style.maxHeight = open.scrollHeight + 'px';
  });

  /* ------------------------------------------------------- Billing toggle */
  const toggle = $('#billingToggle');
  const hint = $('#toggleHint');

  const applyCycle = () => {
    $$('[data-monthly][data-annual]').forEach((el) => {
      const value = cycle === 'annual' ? el.dataset.annual : el.dataset.monthly;
      el.textContent = value;
    });
    if (toggle) {
      toggle.classList.toggle('annual', cycle === 'annual');
      const opts = $$('.bt-opt', toggle);
      opts.forEach((b) => b.classList.toggle('active', b.dataset.cycle === cycle));
      const pill = $('.bt-pill', toggle);
      const active = opts.find((b) => b.dataset.cycle === cycle);
      if (pill && active) {
        pill.style.width = active.offsetWidth + 'px';
        pill.style.transform = `translateX(${active.offsetLeft - 6}px)`;
      }
    }
    if (hint) {
      hint.textContent =
        cycle === 'annual'
          ? 'Annual billing: Pro $9.58/mo and Scale $69.99/mo, billed yearly — credits granted upfront.'
          : 'Switch to annual and Pro drops to $9.58/mo, billed yearly.';
    }
    if (typeof renderEstimator === 'function') renderEstimator();
    document.dispatchEvent(new CustomEvent('cyclechange', { detail: { cycle } }));
  };

  if (toggle) {
    $$('.bt-opt', toggle).forEach((btn) =>
      btn.addEventListener('click', () => {
        cycle = btn.dataset.cycle;
        applyCycle();
      })
    );
  }

  /* ------------------------------------------------------------ Countdown */
  const cd = $('#countdown');
  if (cd) {
    const KEY = 'wv_promo_deadline';
    const DURATION = 3 * 24 * 60 * 60 * 1000; // 3-day rolling offer
    let deadline = parseInt(localStorage.getItem(KEY) || '0', 10);
    if (!deadline || deadline < Date.now()) {
      deadline = Date.now() + DURATION;
      localStorage.setItem(KEY, String(deadline));
    }

    const pad = (n) => String(n).padStart(2, '0');
    const tick = () => {
      let diff = Math.max(0, deadline - Date.now());
      const days = Math.floor(diff / 86400000);
      diff -= days * 86400000;
      const hours = Math.floor(diff / 3600000);
      diff -= hours * 3600000;
      const mins = Math.floor(diff / 60000);
      diff -= mins * 60000;
      const secs = Math.floor(diff / 1000);

      $('#cdDays').textContent = pad(days);
      $('#cdHours').textContent = pad(hours);
      $('#cdMins').textContent = pad(mins);
      $('#cdSecs').textContent = pad(secs);

      if (deadline - Date.now() <= 0) {
        deadline = Date.now() + DURATION;
        localStorage.setItem(KEY, String(deadline));
      }
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ---------------------------------------------------------- Estimator */
  const range = $('#minutesRange');
  let renderEstimator = null;

  if (range) {
    renderEstimator = () => {
      const minutes = parseInt(range.value, 10);
      const isAnnual = cycle === 'annual';

      const plans = [
        { name: 'Free', cap: 100, price: 0, cta: 'Get Started With Free Plan' },
        { name: 'Pro', cap: isAnnual ? 12000 : 1000, price: isAnnual ? 9.58 : 26.99, cta: 'Start with Pro' },
        { name: 'Scale', cap: isAnnual ? 72000 : 6000, price: isAnnual ? 69.99 : 139.99, cta: 'Start with Scale' },
        { name: 'Enterprise', cap: Infinity, price: null, cta: 'Contact Sales' }
      ];

      const pick = plans.find((p) => minutes <= p.cap) || plans[plans.length - 1];

      $('#minutesOut').textContent = minutes.toLocaleString();
      $('#estName').textContent = pick.name;
      $('#estUsage').textContent = minutes.toLocaleString() + ' credits';

      if (pick.price === null) {
        $('#estPrice').textContent = 'Custom pricing';
        $('#estLeft').textContent = 'Custom credit pool';
        $('#estCost').textContent = 'Volume pricing';
      } else if (pick.price === 0) {
        $('#estPrice').textContent = '$0 forever';
        $('#estLeft').textContent = Math.max(0, 100 - minutes).toLocaleString() + ' credits left';
        $('#estCost').textContent = '$0.00 / min';
      } else {
        $('#estPrice').textContent = '$' + pick.price.toFixed(2) + ' / month' + (isAnnual ? ', billed yearly' : '');
        $('#estLeft').textContent = (pick.cap - minutes).toLocaleString() + ' credits left';
        $('#estCost').textContent = '$' + (pick.price / minutes).toFixed(3) + ' / min';
      }

      const cta = $('#estCta');
      if (cta) cta.textContent = pick.cta;
    };

    range.addEventListener('input', renderEstimator);
    renderEstimator();
    document.addEventListener('cyclechange', renderEstimator);
  }

  /* ---------------------------------------------------------------- Year */
  const yearEl = $('#year');
  if (yearEl) yearEl.textContent = new Date().getFullYear();

  // expose cycle for debugging
  applyCycle();
})();
