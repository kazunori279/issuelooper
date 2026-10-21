/**
 * Weather Card Component
 * Requirements: R-WX-1, R-WX-2, R-LANG-1
 */

import { STRINGS } from "../i18n.js";

const JA_WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];
const EN_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * Returns an appropriate weather icon for a condition and day/night state.
 */
function getWeatherIcon(condition, isDay = true) {
  switch (condition) {
    case "clear":
      return isDay ? "☀️" : "🌙";
    case "partly_cloudy":
      return isDay ? "⛅" : "☁️";
    case "cloudy":
      return "☁️";
    case "fog":
      return "🌫️";
    case "drizzle":
      return "🌦️";
    case "rain":
      return "🌧️";
    case "snow":
      return "❄️";
    case "thunderstorm":
      return "⛈️";
    default:
      return isDay ? "☀️" : "🌙";
  }
}

/**
 * Builds the dynamic animated sky background DOM element.
 */
function createSkyBackground(condition, isDay) {
  const bg = document.createElement("div");
  bg.className = "weather-sky-bg";

  let skyClass = "sky-clear-day";
  if (condition === "clear") {
    skyClass = isDay ? "sky-clear-day" : "sky-clear-night";
  } else if (condition === "partly_cloudy") {
    skyClass = isDay ? "sky-partly-cloudy-day" : "sky-partly-cloudy-night";
  } else if (condition === "cloudy" || condition === "fog") {
    skyClass = "sky-cloudy";
  } else if (condition === "rain" || condition === "drizzle") {
    skyClass = "sky-rain";
  } else if (condition === "snow") {
    skyClass = "sky-snow";
  } else if (condition === "thunderstorm") {
    skyClass = "sky-thunderstorm";
  }
  bg.classList.add(skyClass);

  // Background elements based on condition
  if (condition === "clear" || condition === "partly_cloudy") {
    if (isDay) {
      const sun = document.createElement("div");
      sun.className = "sky-celestial sun-orb";
      bg.appendChild(sun);
    } else {
      const moon = document.createElement("div");
      moon.className = "sky-celestial moon-orb";
      bg.appendChild(moon);

      const stars = document.createElement("div");
      stars.className = "sky-stars";
      bg.appendChild(stars);
    }
  }

  if (condition === "partly_cloudy" || condition === "cloudy" || condition === "fog") {
    const clouds = document.createElement("div");
    clouds.className = "sky-clouds";
    clouds.innerHTML = '<div class="cloud c1"></div><div class="cloud c2"></div>';
    bg.appendChild(clouds);
  }

  if (condition === "rain" || condition === "drizzle") {
    const rain = document.createElement("div");
    rain.className = "sky-rain";
    rain.innerHTML = `
      <div class="rain-drop rd1"></div>
      <div class="rain-drop rd2"></div>
      <div class="rain-drop rd3"></div>
      <div class="rain-drop rd4"></div>
      <div class="rain-drop rd5"></div>
    `;
    bg.appendChild(rain);
  }

  if (condition === "snow") {
    const snow = document.createElement("div");
    snow.className = "sky-snow";
    snow.innerHTML = `
      <div class="snowflake sf1">❄</div>
      <div class="snowflake sf2">❅</div>
      <div class="snowflake sf3">❄</div>
      <div class="snowflake sf4">❅</div>
      <div class="snowflake sf5">❄</div>
    `;
    bg.appendChild(snow);
  }

  if (condition === "thunderstorm") {
    const thunder = document.createElement("div");
    thunder.className = "sky-thunder";
    thunder.innerHTML = `
      <div class="lightning-flash"></div>
      <div class="sky-rain">
        <div class="rain-drop rd1"></div>
        <div class="rain-drop rd2"></div>
        <div class="rain-drop rd3"></div>
        <div class="rain-drop rd4"></div>
      </div>
    `;
    bg.appendChild(thunder);
  }

  return bg;
}

/**
 * Renders a Weather Card component per REQUIREMENTS §4 (R-WX-1, R-WX-2, R-LANG-1).
 *
 * @param {Object} result - Weather tool result object.
 * @param {HTMLElement} [container] - Optional parent container to append card to.
 * @returns {HTMLElement} The created card element.
 */
export function renderWeatherCard(result, container) {
  if (!result) return null;

  const isJa = result.lang === "ja" || (result.lang && result.lang.startsWith("ja"));
  const langStrings = isJa ? STRINGS.ja : STRINGS.en;
  const current = result.current || {};
  const isDay = current.is_day !== undefined ? !!current.is_day : true;
  const condition = current.condition || "clear";

  const card = document.createElement("div");
  card.className = "card weather-card";
  card.setAttribute("data-testid", "card");
  card.setAttribute("data-card-type", "weather");

  // Sky background
  const skyBg = createSkyBackground(condition, isDay);
  card.appendChild(skyBg);

  // Check reduced motion preference
  if (typeof window !== "undefined" && window.matchMedia) {
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (mediaQuery.matches) {
      card.classList.add("reduced-motion");
    }
    const motionListener = (e) => {
      card.classList.toggle("reduced-motion", e.matches);
    };
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener("change", motionListener);
    } else if (mediaQuery.addListener) {
      mediaQuery.addListener(motionListener);
    }
  }

  // IntersectionObserver to pause animations when card is out of view
  if (typeof IntersectionObserver !== "undefined") {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            card.classList.remove("animation-paused");
          } else {
            card.classList.add("animation-paused");
          }
        }
      },
      { threshold: 0.05 }
    );
    observer.observe(card);
  }

  // Content container
  const content = document.createElement("div");
  content.className = "weather-content";

  // 1. Header: Location icon, location name, and local time
  const header = document.createElement("div");
  header.className = "weather-header";
  const locationIcon = document.createElement("span");
  locationIcon.className = "weather-location-icon";
  locationIcon.textContent = "📍";
  header.appendChild(locationIcon);

  const locationText = document.createElement("span");
  locationText.className = "weather-location-name";
  locationText.textContent = result.location || "";
  header.appendChild(locationText);

  if (result.local_time) {
    const timeText = document.createElement("span");
    timeText.className = "weather-local-time";
    timeText.textContent = ` ${result.local_time}`;
    header.appendChild(timeText);
  }
  content.appendChild(header);

  // 2. Current conditions: Temp, icon, condition text, Feels like
  const currentSection = document.createElement("div");
  currentSection.className = "weather-current";

  const tempRow = document.createElement("div");
  tempRow.className = "weather-temp-row";

  const tempVal = current.temp_c !== undefined ? Math.round(current.temp_c) : "--";
  const tempLarge = document.createElement("span");
  tempLarge.className = "weather-temp-large";
  tempLarge.textContent = `${tempVal}°`;
  tempRow.appendChild(tempLarge);

  const tempUnit = document.createElement("span");
  tempUnit.className = "weather-temp-unit";
  tempUnit.textContent = "C";
  tempRow.appendChild(tempUnit);
  currentSection.appendChild(tempRow);

  const conditionRow = document.createElement("div");
  conditionRow.className = "weather-condition-row";

  const condIcon = document.createElement("span");
  condIcon.className = "weather-condition-icon";
  condIcon.textContent = getWeatherIcon(condition, isDay);
  conditionRow.appendChild(condIcon);

  const condLabel = document.createElement("span");
  condLabel.className = "weather-condition-label";
  condLabel.textContent =
    (langStrings.conditions && langStrings.conditions[condition]) ||
    condition;
  conditionRow.appendChild(condLabel);
  currentSection.appendChild(conditionRow);

  if (current.feels_like_c !== undefined) {
    const feelsLike = document.createElement("div");
    feelsLike.className = "weather-feels-like";
    const feelsRounded = Math.round(current.feels_like_c);
    const prefix = isJa ? "体感 " : "Feels like ";
    feelsLike.textContent = `${prefix}${feelsRounded}°`;
    currentSection.appendChild(feelsLike);
  }
  content.appendChild(currentSection);

  // 3. Hourly strip (12 entries)
  if (Array.isArray(result.hourly) && result.hourly.length > 0) {
    const hourlyContainer = document.createElement("div");
    hourlyContainer.className = "weather-hourly";

    // Show up to 12 entries
    const hours = result.hourly.slice(0, 12);
    for (const h of hours) {
      const hourItem = document.createElement("div");
      hourItem.className = "weather-hourly-item";
      hourItem.setAttribute("data-testid", "wx-hour");

      const timeEl = document.createElement("span");
      timeEl.className = "weather-hourly-time";
      timeEl.textContent = h.time || "";
      hourItem.appendChild(timeEl);

      const iconEl = document.createElement("span");
      iconEl.className = "weather-hourly-icon";
      iconEl.textContent = getWeatherIcon(h.condition || "clear", isDay);
      hourItem.appendChild(iconEl);

      const tVal = h.temp_c !== undefined ? Math.round(h.temp_c) : "--";
      const hTempEl = document.createElement("span");
      hTempEl.className = "weather-hourly-temp";
      hTempEl.textContent = `${tVal}°`;
      hourItem.appendChild(hTempEl);

      hourlyContainer.appendChild(hourItem);
    }
    content.appendChild(hourlyContainer);
  }

  // 4. 5-Day forecast list (5 entries)
  if (Array.isArray(result.daily) && result.daily.length > 0) {
    const dailyContainer = document.createElement("div");
    dailyContainer.className = "weather-daily";

    const days = result.daily.slice(0, 5);
    days.forEach((d, index) => {
      const dayRow = document.createElement("div");
      dayRow.className = "weather-daily-row";
      dayRow.setAttribute("data-testid", "wx-day");

      const leftSide = document.createElement("div");
      leftSide.className = "weather-daily-left";

      // Day label: Row 0 is Today/今日, Row 1 is Tomorrow/明日, Rows 2-4 is weekday
      const dayLabel = document.createElement("span");
      dayLabel.className = "weather-daily-label";
      if (index === 0) {
        dayLabel.textContent = isJa ? "今日" : "Today";
      } else if (index === 1) {
        dayLabel.textContent = isJa ? "明日" : "Tomorrow";
      } else {
        let weekdayName = "";
        if (d.date) {
          const parts = d.date.split("-").map(Number);
          if (parts.length === 3) {
            const dateObj = new Date(parts[0], parts[1] - 1, parts[2]);
            const dayOfWeek = dateObj.getDay();
            weekdayName = isJa ? JA_WEEKDAYS[dayOfWeek] : EN_WEEKDAYS[dayOfWeek];
          }
        }
        dayLabel.textContent = weekdayName;
      }
      leftSide.appendChild(dayLabel);

      const dayIcon = document.createElement("span");
      dayIcon.className = "weather-daily-icon";
      dayIcon.textContent = getWeatherIcon(d.condition || "clear", true);
      leftSide.appendChild(dayIcon);

      dayRow.appendChild(leftSide);

      const tempsContainer = document.createElement("div");
      tempsContainer.className = "weather-daily-temps";

      const highVal = d.high_c !== undefined ? Math.round(d.high_c) : "--";
      const highEl = document.createElement("span");
      highEl.className = "wx-high";
      highEl.textContent = `${highVal}°`;
      tempsContainer.appendChild(highEl);

      const sep = document.createElement("span");
      sep.className = "wx-sep";
      sep.textContent = " / ";
      tempsContainer.appendChild(sep);

      const lowVal = d.low_c !== undefined ? Math.round(d.low_c) : "--";
      const lowEl = document.createElement("span");
      lowEl.className = "wx-low";
      lowEl.textContent = `${lowVal}°`;
      tempsContainer.appendChild(lowEl);

      dayRow.appendChild(tempsContainer);
      dailyContainer.appendChild(dayRow);
    });

    content.appendChild(dailyContainer);
  }

  // 5. Footer metrics: Wind, Humidity, Sunrise, Sunset
  const footerMetrics = [];
  if (current.wind_kmh !== undefined) {
    footerMetrics.push(`Wind ${current.wind_kmh} km/h`);
  }
  if (current.humidity !== undefined) {
    footerMetrics.push(`Humidity ${current.humidity}%`);
  }
  if (result.sunrise) {
    footerMetrics.push(`Sunrise ${result.sunrise}`);
  }
  if (result.sunset) {
    footerMetrics.push(`Sunset ${result.sunset}`);
  }

  if (footerMetrics.length > 0) {
    const footer = document.createElement("div");
    footer.className = "weather-footer";
    for (const metric of footerMetrics) {
      const metricEl = document.createElement("span");
      metricEl.className = "weather-metric";
      metricEl.textContent = metric;
      footer.appendChild(metricEl);
    }
    content.appendChild(footer);
  }

  card.appendChild(content);

  if (container) {
    container.appendChild(card);
  }

  return card;
}
