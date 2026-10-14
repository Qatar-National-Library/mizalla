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

  // ─── Utility Feedback & Suggestions Modal ───────────────────────────────
  const feedbackModal = document.querySelector("#feedback-modal");
  const openFeedbackBtns = document.querySelectorAll("#open-feedback-btn, #dropdown-feedback-btn, #hamburger-feedback-btn");
  const closeFeedbackBtn = document.querySelector("#feedback-modal-close");
  const cancelFeedbackBtn = document.querySelector("#feedback-cancel-btn");
  const tabExisting = document.querySelector("#tab-feedback-existing");
  const tabNew = document.querySelector("#tab-feedback-new");
  const sectionExisting = document.querySelector("#section-feedback-existing");
  const sectionNew = document.querySelector("#section-feedback-new");
  const targetTypeInput = document.querySelector("#feedback-target-type");
  const appSelect = document.querySelector("#feedback-app-select");
  const statusNote = document.querySelector("#feedback-app-status-note");
  const ratingInput = document.querySelector("#feedback-rating-input");
  const ratingPillBtns = document.querySelectorAll(".rating-pill-btn");
  const commentsExisting = document.querySelector("#feedback-comments-existing");
  const appTitleInput = document.querySelector("#feedback-app-title");
  const commentsNew = document.querySelector("#feedback-comments-new");
  const feedbackForm = document.querySelector("#feedback-form");
  const feedbackSubmitBtn = document.querySelector("#feedback-submit-btn");
  const feedbackErrorMsg = document.querySelector("#feedback-error-msg");
  const feedbackSuccessMsg = document.querySelector("#feedback-success-msg");

  let accessibleAppsCache = null;

  const setRating = (val) => {
    ratingInput.value = val || "";
    ratingPillBtns.forEach((btn) => {
      const btnVal = btn.getAttribute("data-value");
      if (val && btnVal === String(val)) {
        btn.classList.add("active");
        btn.style.background = "#1a73e8";
        btn.style.color = "#ffffff";
        btn.style.borderColor = "#1a73e8";
      } else {
        btn.classList.remove("active");
        btn.style.background = "#fdfdfd";
        btn.style.color = "inherit";
        btn.style.borderColor = "var(--border-color, #ccc)";
      }
    });
  };

  ratingPillBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      setRating(btn.getAttribute("data-value"));
      if (feedbackErrorMsg) feedbackErrorMsg.hidden = true;
    });
  });

  const switchTab = (type) => {
    targetTypeInput.value = type;
    if (type === "existing") {
      tabExisting?.classList.add("active");
      tabExisting?.classList.remove("button-quiet");
      tabNew?.classList.remove("active");
      tabNew?.classList.add("button-quiet");
      if (sectionExisting) sectionExisting.hidden = false;
      if (sectionNew) sectionNew.hidden = true;
    } else {
      tabNew?.classList.add("active");
      tabNew?.classList.remove("button-quiet");
      tabExisting?.classList.remove("active");
      tabExisting?.classList.add("button-quiet");
      if (sectionExisting) sectionExisting.hidden = true;
      if (sectionNew) sectionNew.hidden = false;
    }
    if (feedbackErrorMsg) feedbackErrorMsg.hidden = true;
    if (feedbackSuccessMsg) feedbackSuccessMsg.hidden = true;
  };

  tabExisting?.addEventListener("click", () => switchTab("existing"));
  tabNew?.addEventListener("click", () => switchTab("new"));

  const loadAccessibleApps = async () => {
    if (!appSelect) return;
    try {
      const resp = await fetch("/apps/feedback/accessible-apps", {
        headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" }
      });
      if (!resp.ok) return;
      const data = await resp.json();
      accessibleAppsCache = data.apps || [];

      // Clear existing options except default
      appSelect.innerHTML = '<option value="">-- Choose an application you have access to --</option>';
      accessibleAppsCache.forEach((app) => {
        const opt = document.createElement("option");
        opt.value = app.feature_key;
        opt.textContent = app.label + (app.current_rating ? ` (Rated: ${app.current_rating}/10)` : "");
        appSelect.appendChild(opt);
      });
    } catch {
      // Ignore background fetch error
    }
  };

  appSelect?.addEventListener("change", () => {
    const selectedKey = appSelect.value;
    if (!selectedKey || !accessibleAppsCache) {
      setRating("");
      if (commentsExisting) commentsExisting.value = "";
      if (statusNote) {
        statusNote.hidden = true;
        statusNote.textContent = "";
      }
      return;
    }
    const found = accessibleAppsCache.find((a) => a.feature_key === selectedKey);
    if (found && found.current_rating) {
      setRating(found.current_rating);
      if (commentsExisting) commentsExisting.value = found.current_comments || "";
      if (statusNote) {
        statusNote.hidden = false;
        statusNote.textContent = `You previously rated this app ${found.current_rating}/10 on ${found.updated_at || "earlier"}. Submitting will update your review.`;
      }
    } else {
      setRating("");
      if (commentsExisting) commentsExisting.value = "";
      if (statusNote) {
        statusNote.hidden = true;
        statusNote.textContent = "";
      }
    }
    if (feedbackErrorMsg) feedbackErrorMsg.hidden = true;
  });

  const openModal = () => {
    if (!feedbackModal) return;
    if (feedbackErrorMsg) feedbackErrorMsg.hidden = true;
    if (feedbackSuccessMsg) feedbackSuccessMsg.hidden = true;
    loadAccessibleApps();
    if (typeof feedbackModal.showModal === "function") {
      feedbackModal.showModal();
    } else {
      feedbackModal.setAttribute("open", "");
    }
    if (window.lucide) {
      lucide.createIcons();
    }
  };

  const closeModal = () => {
    if (!feedbackModal) return;
    if (typeof feedbackModal.close === "function") {
      feedbackModal.close();
    } else {
      feedbackModal.removeAttribute("open");
    }
  };

  openFeedbackBtns.forEach((btn) => btn.addEventListener("click", openModal));
  closeFeedbackBtn?.addEventListener("click", closeModal);
  cancelFeedbackBtn?.addEventListener("click", closeModal);

  // Auto-open modal if requested in URL query string (e.g. from /apps/feedback redirect)
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get("open_feedback") === "true") {
    openModal();
  }

  feedbackModal?.addEventListener("click", (e) => {
    const rect = feedbackModal.getBoundingClientRect();
    const isInDialog = (
      rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
      rect.left <= e.clientX && e.clientX <= rect.left + rect.width
    );
    if (!isInDialog) closeModal();
  });

  feedbackForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (feedbackErrorMsg) feedbackErrorMsg.hidden = true;
    if (feedbackSuccessMsg) feedbackSuccessMsg.hidden = true;

    const targetType = targetTypeInput.value;
    if (targetType === "existing") {
      if (!appSelect.value) {
        feedbackErrorMsg.textContent = "Please select a utility application you have access to.";
        feedbackErrorMsg.hidden = false;
        return;
      }
      if (!ratingInput.value) {
        feedbackErrorMsg.textContent = "Please select a rating between 1 and 10.";
        feedbackErrorMsg.hidden = false;
        return;
      }
    } else {
      if (!appTitleInput.value.trim()) {
        feedbackErrorMsg.textContent = "Please provide a name or title for the requested utility app.";
        feedbackErrorMsg.hidden = false;
        return;
      }
      if (!commentsNew.value.trim()) {
        feedbackErrorMsg.textContent = "Please describe what this new utility app should do.";
        feedbackErrorMsg.hidden = false;
        return;
      }
    }

    const formData = new FormData(feedbackForm);
    if (feedbackSubmitBtn) feedbackSubmitBtn.disabled = true;

    try {
      const resp = await fetch(feedbackForm.action, {
        method: "POST",
        body: formData,
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json"
        }
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || "Failed to submit feedback.");
      }

      feedbackSuccessMsg.textContent = data.message || "Thank you! Your feedback has been recorded.";
      feedbackSuccessMsg.hidden = false;

      // Update cached values
      if (targetType === "existing" && accessibleAppsCache) {
        const found = accessibleAppsCache.find((a) => a.feature_key === appSelect.value);
        if (found) {
          found.current_rating = parseInt(ratingInput.value, 10);
          found.current_comments = commentsExisting.value;
        }
      }

      setTimeout(() => {
        closeModal();
        if (targetType === "new") {
          appTitleInput.value = "";
          commentsNew.value = "";
        }
      }, 1500);
    } catch (err) {
      feedbackErrorMsg.textContent = err.message || "Error submitting feedback.";
      feedbackErrorMsg.hidden = false;
    } finally {
      if (feedbackSubmitBtn) feedbackSubmitBtn.disabled = false;
    }
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