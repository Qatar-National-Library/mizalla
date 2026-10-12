document.addEventListener("DOMContentLoaded", () => {
  // Hamburger menu toggle
  const hamburger = document.querySelector("#hamburger");
  const menu = document.querySelector("#hamburger-menu");
  if (hamburger && menu) {
    hamburger.addEventListener("click", () => {
      const isOpen = !menu.hidden;
      menu.hidden = isOpen;
      hamburger.setAttribute("aria-expanded", String(!isOpen));
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && e.target !== hamburger) {
        menu.hidden = true;
        hamburger.setAttribute("aria-expanded", "false");
      }
    });
  }

  // User dropdown toggle
  const userToggle = document.querySelector("#user-dropdown-toggle");
  const userMenu = document.querySelector("#user-dropdown-menu");
  if (userToggle && userMenu) {
    userToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !userMenu.hidden;
      userMenu.hidden = isOpen;
      userToggle.setAttribute("aria-expanded", String(!isOpen));
    });
    document.addEventListener("click", (e) => {
      if (!userMenu.contains(e.target) && e.target !== userToggle) {
        userMenu.hidden = true;
        userToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  document.querySelectorAll("form[data-meeting-form]").forEach((form) => {
    const dateField = form.querySelector('[name="meeting_date"]');
    const timeField = form.querySelector('[name="meeting_time"]');
    const offsetField = form.querySelector('[name="meeting_utc_offset_minutes"]');
    if (!dateField || !timeField || !offsetField) return;

    const updateOffset = () => {
      if (!dateField.value || !timeField.value) {
        offsetField.value = "";
        return;
      }
      const localDateTime = new Date(`${dateField.value}T${timeField.value}:00`);
      offsetField.value = Number.isNaN(localDateTime.getTime()) ? "" : localDateTime.getTimezoneOffset();
    };

    dateField.addEventListener("change", updateOffset);
    timeField.addEventListener("change", updateOffset);
    form.addEventListener("submit", () => {
      updateOffset();
      const localDateTime = dateField.value && timeField.value
        ? new Date(`${dateField.value}T${timeField.value}:00`)
        : null;
      if (localDateTime && localDateTime.getTime() > Date.now()) {
        form.target = "_blank";
      } else {
        form.removeAttribute("target");
      }
    });
  });
});

// Shared by the Bulk Emailer, Bulk SMS and Bulk Messenger composers.
//
// They post with fetch() and read response.json() unconditionally, so any HTML
// answer - a styled error page, a redirect to the login form, a CSRF rejection -
// surfaced as "Unexpected token '<'" and the real reason was lost. This marks the
// request so the server answers in JSON, and reads the body as text first so a
// stray page still yields a readable message rather than a parse error.
window.composerFetch = async (url, options = {}) => {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Requested-With": "XMLHttpRequest",
      Accept: "application/json",
      ...(options.headers || {}),
    },
  });

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!payload) {
    // The server sent a page rather than JSON. That means something outside the
    // app answered - a proxy, a load balancer, an expired session caught upstream.
    throw new Error(
      response.status === 401
        ? "Your session expired. Refresh the page and sign in again."
        : `The server returned a web page instead of a response (${response.status}). Check your connection, then refresh and try again.`,
    );
  }

  if (!response.ok) {
    throw new Error(payload.error || "The request failed.");
  }

  return payload;
};