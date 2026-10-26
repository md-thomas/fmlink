#!/usr/bin/env python3
"""
Plot a constellation (I vs Q scatter) from a raw complex symbol file - e.g.
modulate.py's direct output, or matchedfilter.py's decimated output after a
pulse-shape/matched-filter round trip. Produces a self-contained interactive
HTML file (Plotly) instead of a static image, so it opens in any browser -
no display/X11 forwarding needed on a remote box - with real zoom/pan and
hover tooltips showing each point's exact I/Q value and index.

    python3 plot_constellation.py -i test.cfile -o constellation.html

Note: a file straight out of pulseshape.py (oversampled, not yet matched-
filtered/decimated) will NOT show a clean constellation here - see
matchedfilter.py first. This tool just plots whatever complex points are in
the file; it doesn't know or care which pipeline stage produced them.
"""
import argparse

import plotly.graph_objects as go

import fmlink_modem as modem


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-i', '--input', required=True,
                         help='Input raw complex64 (.cfile) symbol file')
    parser.add_argument('-o', '--output', default='constellation.html',
                         help='Output HTML path (default: constellation.html)')
    parser.add_argument('--title', default=None, help='Plot title (default: input filename)')
    parser.add_argument('--limit', type=float, default=None,
                         help='Fixed +/- axis limit for both I and Q (default: auto - '
                              '1.5x the largest magnitude in the file, matching the '
                              ".grc flowgraphs' QT GUI Constellation Sink convention)")
    parser.add_argument('--max-points', type=int, default=5000,
                         help='If more symbols than this are in the file, evenly '
                              'subsample down to this many for the plot - a real file '
                              'transfer can have hundreds of thousands of symbols '
                              '(millions after pulse-shaping oversampling), and plotting '
                              'every single one produces a multi-hundred-MB HTML file '
                              'that browsers struggle to render. The constellation shape '
                              '(what this plot is actually for) is already clear from a '
                              'representative subsample. Default: 5000, 0 disables '
                              'subsampling and plots every point.')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        symbols = modem.iq_bytes_to_symbols(f.read())

    limit = args.limit
    if limit is None:
        max_mag = max((abs(s) for s in symbols), default=1.0)
        limit = max(max_mag * 1.5, 0.1)

    total = len(symbols)
    indices = range(total)
    if args.max_points and total > args.max_points:
        stride = -(-total // args.max_points)  # ceil division
        indices = range(0, total, stride)

    i_vals = [symbols[n].real for n in indices]
    q_vals = [symbols[n].imag for n in indices]
    hover_text = [f"#{n}: {symbols[n].real:+.4f} {symbols[n].imag:+.4f}j  |s|={abs(symbols[n]):.4f}"
                  for n in indices]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=i_vals, y=q_vals, mode='markers',
        marker=dict(size=6, opacity=0.6),
        text=hover_text, hoverinfo='text',
    ))
    fig.add_hline(y=0, line_color='gray', line_width=1)
    fig.add_vline(x=0, line_color='gray', line_width=1)
    fig.update_xaxes(title='I', range=[-limit, limit], zeroline=False,
                      scaleanchor='y', scaleratio=1)
    fig.update_yaxes(title='Q', range=[-limit, limit], zeroline=False)
    fig.update_layout(
        title=args.title or args.input,
        width=700, height=700,
        template='plotly_white',
    )

    fig.write_html(args.output, include_plotlyjs=True)

    if len(indices) < total:
        print(f"Wrote {len(indices)}-point interactive constellation plot "
              f"(subsampled from {total} symbols, --max-points {args.max_points}) to {args.output}")
    else:
        print(f"Wrote {total}-point interactive constellation plot to {args.output}")


if __name__ == '__main__':
    main()
