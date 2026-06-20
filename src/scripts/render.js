(function () {
  var SVG_NS = "http://www.w3.org/2000/svg";

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function formatNumber(value, digits) {
    return Number(value).toFixed(digits);
  }

  function setText(element, text) {
    if (element) {
      element.textContent = text;
    }
  }

  function resolveScale(series, explicitMin, explicitMax) {
    var min = explicitMin;
    var max = explicitMax;

    if (min == null || max == null) {
      min = Math.min.apply(null, series);
      max = Math.max.apply(null, series);

      var range = max - min;
      var padding = range > 0 ? range * 0.28 : Math.max(Math.abs(max) * 0.08, 1);

      min -= padding;
      max += padding;
    }

    if (max - min < 0.001) {
      max += 1;
      min -= 1;
    }

    return { min: min, max: max };
  }

  function toCoordinates(series, options) {
    var width = options.width;
    var height = options.height;
    var paddingTop = options.paddingTop || 0;
    var paddingBottom = options.paddingBottom || 0;
    var plotHeight = height - paddingTop - paddingBottom;
    var scale = resolveScale(series, options.min, options.max);
    var lastIndex = Math.max(series.length - 1, 1);

    return series.map(function (value, index) {
      var ratio = clamp((value - scale.min) / (scale.max - scale.min), 0, 1);
      var x = (index / lastIndex) * width;
      var y = paddingTop + (1 - ratio) * plotHeight;

      return {
        x: roundForSvg(x),
        y: roundForSvg(y),
      };
    });
  }

  function roundForSvg(value) {
    return Math.round(value * 10) / 10;
  }

  function pointsToString(points) {
    return points
      .map(function (point) {
        return point.x + "," + point.y;
      })
      .join(" ");
  }

  function renderSparkline(polyline, series, scale) {
    if (!polyline || !series || !series.length) {
      return;
    }

    var points = toCoordinates(series, {
      width: 180,
      height: 44,
      min: scale && scale.min,
      max: scale && scale.max,
      paddingTop: 5,
      paddingBottom: 7,
    });

    polyline.setAttribute("points", pointsToString(points));
  }

  function renderHistory(container, series, maxValue, maxLabel, ariaLabel) {
    if (!container || !series || !series.length) {
      return;
    }

    var width = 360;
    var height = 126;
    var paddingBottom = 14;
    var baseline = height - paddingBottom;
    var points = toCoordinates(series, {
      width: width,
      height: height,
      min: 0,
      max: maxValue,
      paddingTop: 8,
      paddingBottom: paddingBottom,
    });
    var pointString = pointsToString(points);
    var area = "0," + baseline + " " + pointString + " " + width + "," + baseline;
    var areaElement = container.querySelector("[data-history-area]");
    var lineElement = container.querySelector("[data-history-line]");
    var pointGroup = container.querySelector("[data-history-points]");

    setText(container.querySelector("[data-history-max]"), maxLabel);
    container.setAttribute("aria-label", ariaLabel);

    if (areaElement) {
      areaElement.setAttribute("points", area);
    }

    if (lineElement) {
      lineElement.setAttribute("points", pointString);
    }

    if (pointGroup) {
      pointGroup.textContent = "";

      points.forEach(function (point) {
        var circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("cx", point.x);
        circle.setAttribute("cy", point.y);
        circle.setAttribute("r", "3");
        pointGroup.appendChild(circle);
      });
    }
  }

  function renderGauge(container, percent, mainText, subText, ariaLabel) {
    if (!container) {
      return;
    }

    var clamped = clamp(percent, 0, 100);
    var path = container.querySelector("[data-gauge-value]");

    if (path) {
      path.style.strokeDasharray = formatNumber(clamped, 1) + " 100";
      path.classList.toggle("gauge-value-empty", clamped === 0);
    }

    setText(container.querySelector("[data-gauge-main]"), mainText);
    setText(container.querySelector("[data-gauge-sub]"), subText);
    container.setAttribute("aria-label", ariaLabel);
  }

  function renderMetricValue(valueElement, limitElement, valueText, limitText) {
    setText(valueElement, valueText);
    setText(limitElement, limitText || "");
  }

  window.SparkRender = {
    formatNumber: formatNumber,
    renderGauge: renderGauge,
    renderHistory: renderHistory,
    renderMetricValue: renderMetricValue,
    renderSparkline: renderSparkline,
  };
})();
