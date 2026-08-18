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

import openwave_modem as modem


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
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        symbols = modem.iq_bytes_to_symbols(f.read())

    limit = args.limit
    if limit is None:
        max_mag = max((abs(s) for s in symbols), default=1.0)
        limit = max(max_mag * 1.5, 0.1)

    i_vals = [s.real for s in symbols]
    q_vals = [s.imag for s in symbols]
    hover_text = [f"#{n}: {s.real:+.4f} {s.imag:+.4f}j  |s|={abs(s):.4f}"
                  for n, s in enumerate(symbols)]

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

    print(f"Wrote {len(symbols)}-point interactive constellation plot to {args.output}")


if __name__ == '__main__':
    main()
