/* ECharts 图表封装：均线走势 / 资金流向 / 两融趋势（需求 7.1） */
(function (global) {
  'use strict';

  const UP = '#f5465d';
  const DOWN = '#17b26a';
  const GRID_LINE = '#262d3a';
  const TEXT_DIM = '#9aa4b6';
  const TEXT_FAINT = '#6b7688';

  const MA_COLORS = { MA5: '#f0b90b', MA10: '#3b82f6', MA20: '#a855f7', MA60: '#22d3ee' };

  const instances = [];

  function baseOption() {
    return {
      backgroundColor: 'transparent',
      textStyle: { color: TEXT_DIM, fontFamily: 'inherit' },
      grid: { left: 58, right: 58, top: 34, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(22, 27, 36, .96)',
        borderColor: GRID_LINE,
        borderWidth: 1,
        textStyle: { color: '#e6e9ef', fontSize: 12 },
        axisPointer: { type: 'cross', label: { backgroundColor: '#2b3444' } }
      },
      legend: {
        top: 2,
        textStyle: { color: TEXT_DIM, fontSize: 11 },
        itemWidth: 14,
        itemHeight: 8
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        axisLine: { lineStyle: { color: GRID_LINE } },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 },
        axisTick: { show: false }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: GRID_LINE, type: 'dashed' } },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 }
      }
    };
  }

  function mount(dom, option) {
    if (!dom) return null;
    if (!global.echarts) {
      dom.innerHTML = '<div class="loading-block">图表库未加载，无法渲染曲线</div>';
      return null;
    }
    const existing = global.echarts.getInstanceByDom(dom);
    if (existing) existing.dispose();
    const chart = global.echarts.init(dom, null, { renderer: 'canvas' });
    chart.setOption(option);
    instances.push(chart);
    return chart;
  }

  function disposeAll() {
    while (instances.length) {
      const c = instances.pop();
      try { c.dispose(); } catch (e) { /* ignore */ }
    }
  }

  function resizeAll() {
    instances.forEach(function (c) {
      try { c.resize(); } catch (e) { /* ignore */ }
    });
  }

  /** 收盘价 + 四条均线 */
  function maChart(dom, bars, maSeries) {
    if (!bars || !bars.length) return null;
    const dates = bars.map(function (b) { return b.date; });
    const closes = bars.map(function (b) { return b.close; });

    const series = [{
      name: '收盘价',
      type: 'line',
      data: closes,
      smooth: false,
      symbol: 'none',
      lineStyle: { width: 2, color: '#e6e9ef' },
      areaStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(230, 233, 239, .16)' },
            { offset: 1, color: 'rgba(230, 233, 239, 0)' }
          ]
        }
      },
      z: 3
    }];

    ['MA5', 'MA10', 'MA20', 'MA60'].forEach(function (name) {
      const raw = (maSeries || {})[name];
      if (!raw) return;
      // 后端返回的序列长度与全量 K 线一致，这里对齐到展示窗口
      const data = raw.slice(-bars.length);
      series.push({
        name: name,
        type: 'line',
        data: data,
        smooth: true,
        symbol: 'none',
        connectNulls: false,
        lineStyle: { width: 1.4, color: MA_COLORS[name] },
        z: 2
      });
    });

    const option = baseOption();
    option.xAxis.data = dates;
    option.series = series;
    option.grid.top = 30;
    option.dataZoom = [
      { type: 'inside', start: 50, end: 100 },
      { type: 'slider', start: 50, end: 100, height: 16, bottom: 4,
        borderColor: GRID_LINE, fillerColor: 'rgba(59,130,246,.14)',
        handleStyle: { color: '#3b82f6' }, textStyle: { color: TEXT_FAINT, fontSize: 10 } }
    ];
    option.grid.bottom = 46;
    option.tooltip.valueFormatter = function (v) {
      return v == null ? '--' : Number(v).toFixed(2);
    };
    return mount(dom, option);
  }

  /** 资金流向：逐日主力净额柱 + 累计净额线 */
  function flowChart(dom, rows, tiered) {
    if (!rows || !rows.length) return null;
    const dates = rows.map(function (r) { return r.date.slice(5); });
    let cumulative = 0;
    const cum = rows.map(function (r) { cumulative += r.main; return +(cumulative / 1e8).toFixed(4); });

    const series = [{
      name: '主力净额(亿)',
      type: 'bar',
      data: rows.map(function (r) { return +(r.main / 1e8).toFixed(4); }),
      itemStyle: {
        color: function (p) { return p.value >= 0 ? UP : DOWN; }
      },
      barMaxWidth: 16,
      yAxisIndex: 0
    }, {
      name: '累计净额(亿)',
      type: 'line',
      data: cum,
      smooth: true,
      symbol: 'none',
      yAxisIndex: 1,
      lineStyle: { width: 2, color: '#f0b90b' },
      z: 5
    }];

    if (tiered) {
      series.push({
        name: '超大单(亿)',
        type: 'line',
        data: rows.map(function (r) { return +(r.xl / 1e8).toFixed(4); }),
        smooth: true, symbol: 'none', yAxisIndex: 0,
        lineStyle: { width: 1.3, color: '#a855f7', type: 'dashed' }
      });
      series.push({
        name: '小单(亿)',
        type: 'line',
        data: rows.map(function (r) { return +(r.sm / 1e8).toFixed(4); }),
        smooth: true, symbol: 'none', yAxisIndex: 0,
        lineStyle: { width: 1.3, color: '#22d3ee', type: 'dashed' }
      });
    }

    const option = baseOption();
    option.xAxis.boundaryGap = true;
    option.xAxis.data = dates;
    option.yAxis = [
      {
        type: 'value', name: '单日', nameTextStyle: { color: TEXT_FAINT, fontSize: 10 },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: GRID_LINE, type: 'dashed' } },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 }
      },
      {
        type: 'value', name: '累计', nameTextStyle: { color: TEXT_FAINT, fontSize: 10 },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 }
      }
    ];
    option.series = series;
    option.tooltip.valueFormatter = function (v) {
      return v == null ? '--' : (v > 0 ? '+' : '') + Number(v).toFixed(2) + '亿';
    };
    return mount(dom, option);
  }

  /** 两融：融资余额（亿）+ 融券余额（万） */
  function marginChart(dom, rows) {
    if (!rows || !rows.length) return null;
    const dates = rows.map(function (r) { return r.date.slice(5); });

    const option = baseOption();
    option.xAxis.data = dates;
    option.yAxis = [
      {
        type: 'value', scale: true, name: '融资(亿)',
        nameTextStyle: { color: TEXT_FAINT, fontSize: 10 },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: GRID_LINE, type: 'dashed' } },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 }
      },
      {
        type: 'value', scale: true, name: '融券(万)',
        nameTextStyle: { color: TEXT_FAINT, fontSize: 10 },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { color: TEXT_FAINT, fontSize: 11 }
      }
    ];
    option.series = [
      {
        name: '融资余额(亿)',
        type: 'line',
        data: rows.map(function (r) { return r.rzye == null ? null : +(r.rzye / 1e8).toFixed(4); }),
        smooth: true, symbol: 'none', yAxisIndex: 0,
        lineStyle: { width: 2, color: UP },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(245, 70, 93, .2)' },
              { offset: 1, color: 'rgba(245, 70, 93, 0)' }
            ]
          }
        }
      },
      {
        name: '融券余额(万)',
        type: 'line',
        data: rows.map(function (r) { return r.rqye == null ? null : +(r.rqye / 1e4).toFixed(2); }),
        smooth: true, symbol: 'none', yAxisIndex: 1,
        lineStyle: { width: 1.6, color: DOWN }
      }
    ];
    option.tooltip.valueFormatter = function (v) {
      return v == null ? '--' : Number(v).toFixed(2);
    };
    return mount(dom, option);
  }

  /** 融资买入 / 偿还 双向柱 */
  function marginFlowChart(dom, rows) {
    if (!rows || !rows.length) return null;
    const dates = rows.map(function (r) { return r.date.slice(5); });
    const option = baseOption();
    option.xAxis.boundaryGap = true;
    option.xAxis.data = dates;
    option.yAxis.name = '亿元';
    option.yAxis.nameTextStyle = { color: TEXT_FAINT, fontSize: 10 };
    option.series = [
      {
        name: '融资买入额(亿)',
        type: 'bar',
        stack: 'rz',
        data: rows.map(function (r) { return r.rzmre == null ? null : +(r.rzmre / 1e8).toFixed(4); }),
        itemStyle: { color: 'rgba(245, 70, 93, .8)' },
        barMaxWidth: 14
      },
      {
        name: '融资偿还额(亿)',
        type: 'bar',
        stack: 'rz',
        data: rows.map(function (r) { return r.rzche == null ? null : -(+(r.rzche / 1e8).toFixed(4)); }),
        itemStyle: { color: 'rgba(23, 178, 106, .8)' },
        barMaxWidth: 14
      },
      {
        name: '融券卖出量(万股)',
        type: 'line',
        data: rows.map(function (r) { return r.rqmcl == null ? null : +(r.rqmcl / 1e4).toFixed(2); }),
        smooth: true, symbol: 'none',
        lineStyle: { width: 1.4, color: '#f0b90b' },
        yAxisIndex: 0
      }
    ];
    option.tooltip.valueFormatter = function (v) {
      return v == null ? '--' : Math.abs(Number(v)).toFixed(2);
    };
    return mount(dom, option);
  }

  global.Charts = {
    maChart: maChart,
    flowChart: flowChart,
    marginChart: marginChart,
    marginFlowChart: marginFlowChart,
    disposeAll: disposeAll,
    resizeAll: resizeAll
  };

  global.addEventListener('resize', U.debounce(resizeAll, 180));
})(window);
