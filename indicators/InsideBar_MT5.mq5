//+------------------------------------------------------------------+
//|                                              InsideBar_MT5.mq5   |
//|             Marks Inside Bar patterns on the chart (MetaTrader5) |
//+------------------------------------------------------------------+
#property copyright "hello-cli"
#property link      ""
#property version   "1.00"
#property description "Marks bars whose range is fully contained within the previous bar's range."
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

#property indicator_label1  "Inside Bar (bullish context)"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrDodgerBlue
#property indicator_width1  2

#property indicator_label2  "Inside Bar (bearish context)"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrOrangeRed
#property indicator_width2  2

input int    InpArrowCodeUp     = 233;  // Arrow code, bullish-context inside bar (plotted below the bar)
input int    InpArrowCodeDown   = 234;  // Arrow code, bearish-context inside bar (plotted above the bar)
input double InpArrowGapPoints  = 10;   // Gap between arrow and bar, in points

double UpBuffer[];
double DownBuffer[];

//+------------------------------------------------------------------+
//| Custom indicator initialization function                        |
//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, UpBuffer, INDICATOR_DATA);
   SetIndexBuffer(1, DownBuffer, INDICATOR_DATA);

   PlotIndexSetInteger(0, PLOT_ARROW, InpArrowCodeUp);
   PlotIndexSetInteger(1, PLOT_ARROW, InpArrowCodeDown);

   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   ArraySetAsSeries(UpBuffer, false);
   ArraySetAsSeries(DownBuffer, false);

   IndicatorSetString(INDICATOR_SHORTNAME, "Inside Bar");
   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Custom indicator iteration function                              |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                 const int prev_calculated,
                 const datetime &time[],
                 const double &open[],
                 const double &high[],
                 const double &low[],
                 const double &close[],
                 const long &tick_volume[],
                 const long &volume[],
                 const int &spread[])
{
   if(rates_total < 2)
      return(0);

   int start = (prev_calculated > 1) ? prev_calculated - 1 : 1;

   for(int i = start; i < rates_total; i++)
   {
      UpBuffer[i]   = EMPTY_VALUE;
      DownBuffer[i] = EMPTY_VALUE;

      // Inside bar: current bar's range is fully contained within the previous bar's range
      bool isInside = (high[i] < high[i - 1]) && (low[i] > low[i - 1]);
      if(!isInside)
         continue;

      double gap = InpArrowGapPoints * _Point;
      bool motherBarBullish = close[i - 1] >= open[i - 1];

      if(motherBarBullish)
         UpBuffer[i] = low[i] - gap;
      else
         DownBuffer[i] = high[i] + gap;
   }

   return(rates_total);
}
//+------------------------------------------------------------------+
