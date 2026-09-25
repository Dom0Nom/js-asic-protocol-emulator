/*
 * Tempo lane: a timed line-code engine. Behaviour is specified by
 * docs/architecture.md and modelled cycle for cycle by tools/tempo/lane.py;
 * the next-state block below follows that model's structure on purpose so
 * the two can be compared line by line.
 *
 * Copyright (c) 2026 Dom0Nom
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tempo_lane #(
    parameter FULL = 1
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire [15:0] pins_in,     // synchronised input pins

    // thread operations, valid in the cycle the instruction commits
    input  wire        op_push,
    input  wire [2:0]  op_kind,     // 0 tx, 1 rx, 2 emit, 3 lw, 4 start
    input  wire [15:0] op_data,
    input  wire [4:0]  op_n,
    input  wire [2:0]  op_flags,
    input  wire [3:0]  op_reg,
    input  wire        op_pop,
    input  wire        op_consume_start,
    input  wire        op_consume_stop,
    input  wire [15:0] op_clear,
    input  wire        op_stop,

    // state visible to the core
    input  wire [3:0]  rd_idx,
    output reg  [15:0] rd_data,
    output wire [15:0] status,
    output wire        q_full,
    output wire        idle,
    output wire        rx_avail,
    output wire [15:0] rx_word,
    output wire        rx_end,
    output wire        rx_err,
    output wire        ev_start_o,
    output wire        ev_stop_o,
    output wire [15:0] cap_o,

    // pins
    output wire [15:0] pins_cfg,
    output wire [1:0]  d_o,         // {enable, level}
    output wire [1:0]  a_o,
    output wire [1:0]  c_o,
    output wire [2:0]  owned_o,     // {c, a, d}
    output wire        running_o    // a command is in progress
);
  localparam K_TX = 3'd0, K_RX = 3'd1, K_EMIT = 3'd2, K_LW = 3'd3, K_START = 3'd4;
  localparam SK_TX = 2'd0, SK_RX = 2'd1, SK_EMIT = 2'd2;

  // ------------------------------------------------------------------ state
  reg [13:0] cfg;
  reg [15:0] pins, len0, len1, sym0, sym1, pat, pate, crc, poly, cap;
  reg [7:0]  frac;
  reg [12:0] cr, cf, samp;
  reg [11:0] mode;

  reg        q0_v, q1_v;
  reg [2:0]  q0_kind, q1_kind;
  reg [15:0] q0_data, q1_data;
  reg [4:0]  q0_n, q1_n;
  reg [2:0]  q0_flags, q1_flags;
  reg [3:0]  q0_reg, q1_reg;

  reg        f0_v, f1_v;
  reg [15:0] f0_word, f1_word;
  reg [4:0]  f0_count, f1_count;
  reg        f0_end, f1_end, f0_err, f1_err;

  reg [6:0]  stat;          // sticky bits 3..9
  reg        ev_start, ev_stop;
  reg [4:0]  last_count;
  reg        pend_v;
  reg [15:0] pend_t;

  reg        cur_v;
  reg [2:0]  cur_kind;
  reg [4:0]  cur_n;
  reg [2:0]  cur_flags;
  reg [15:0] cur_data;
  reg [16:0] t, slot_len;
  reg [7:0]  pre;
  reg [7:0]  acc;
  reg        frozen, frozen_done;
  reg [1:0]  slot_kind;
  reg        slot_v, slot_sample;
  reg [4:0]  bits_left;
  reg [15:0] shift;
  reg        tx_last, arb_lost_cmd, rx_complete;

  reg        line, last_bit, stuff_pending;
  reg [2:0]  run;
  reg        rline, rlast, destuff, rerr;
  reg [2:0]  rrun;
  reg [15:0] rword;
  reg [4:0]  rcount;
  reg        prev_level;

  reg [1:0]  d, a, c;       // {enable, level} as {[1], [0]}: [0] level, [1] enable
  reg [2:0]  owned;         // {c, a, d}
  reg [3:0]  filt;
  reg [11:0] fcnt;
  reg [3:0]  prev4;

  // ----------------------------------------------------------- next state
  reg [13:0] cfg_n;
  reg [15:0] pins_n, len0_n, len1_n, sym0_n, sym1_n, pat_n, pate_n, crc_n, poly_n, cap_n;
  reg [7:0]  frac_n;
  reg [12:0] cr_n, cf_n, samp_n;
  reg [11:0] mode_n;
  reg        q0_v_n, q1_v_n;
  reg [2:0]  q0_kind_n, q1_kind_n;
  reg [15:0] q0_data_n, q1_data_n;
  reg [4:0]  q0_n_n, q1_n_n;
  reg [2:0]  q0_flags_n, q1_flags_n;
  reg [3:0]  q0_reg_n, q1_reg_n;
  reg        f0_v_n, f1_v_n;
  reg [15:0] f0_word_n, f1_word_n;
  reg [4:0]  f0_count_n, f1_count_n;
  reg        f0_end_n, f1_end_n, f0_err_n, f1_err_n;
  reg [6:0]  stat_n;
  reg        ev_start_n, ev_stop_n;
  reg [4:0]  last_count_n;
  reg        pend_v_n;
  reg [15:0] pend_t_n;
  reg        cur_v_n;
  reg [2:0]  cur_kind_n;
  reg [4:0]  cur_n_n;
  reg [2:0]  cur_flags_n;
  reg [15:0] cur_data_n;
  reg [16:0] t_n, slot_len_n;
  reg [7:0]  pre_n;
  reg [7:0]  acc_n;
  reg        frozen_n, frozen_done_n;
  reg [1:0]  slot_kind_n;
  reg        slot_v_n, slot_sample_n;
  reg [4:0]  bits_left_n;
  reg [15:0] shift_n;
  reg        tx_last_n, arb_lost_cmd_n, rx_complete_n;
  reg        line_n, last_bit_n, stuff_pending_n;
  reg [2:0]  run_n;
  reg        rline_n, rlast_n, destuff_n, rerr_n;
  reg [2:0]  rrun_n;
  reg [15:0] rword_n;
  reg [4:0]  rcount_n;
  reg        prev_level_n;
  reg [1:0]  d_n, a_n, c_n;
  reg [2:0]  owned_n;
  reg [3:0]  filt_n;
  reg [11:0] fcnt_n;
  reg [3:0]  prev4_n;

  // scratch variables of the procedural model
  reg        set_start, set_stop, done, ended;
  reg        rx_push;
  reg [15:0] rxp_word;
  reg [4:0]  rxp_count;
  reg        rxp_end, rxp_err;
  reg [3:0]  cur4;
  reg [7:0]  pat8;
  reg        in_d, in_i, in_c, in_a, prev_i, raw, x, lvl, cl, clk_rel, m_start, m_stop, fb;
  reg [2:0]  flt, stuff;
  reg [15:0] sym, diff, crc_tmp, w;
  reg [4:0]  k;
  reg [7:0]  off;  // cycles from a pad edge to the cycle after its match
  reg [8:0]  acc_sum;
  reg        has_data, rrun_skip;
  integer    r;


  function [3:0] pin_d;  input [15:0] p; pin_d = p[3:0];   endfunction
  function [3:0] pin_i;  input [15:0] p; pin_i = p[7:4];   endfunction
  function [3:0] pin_c;  input [15:0] p; pin_c = p[11:8];  endfunction
  function [3:0] pin_a;  input [15:0] p; pin_a = p[15:12]; endfunction

  function in_pin;
    input [15:0] pv;
    input [3:0]  idx;
    in_pin = (idx == 4'd15) ? 1'b0 : pv[idx];
  endfunction

  // The procedural model. Tasks write the *_n variables.
  function [15:0] crc_next;
    input [15:0] cv;
    input [15:0] pv;
    input        msb;
    input        b;
    begin
      if (msb) crc_next = {cv[14:0], 1'b0} ^ ((cv[15] ^ b) ? pv : 16'd0);
      else crc_next = {1'b0, cv[15:1]} ^ ((cv[0] ^ b) ? pv : 16'd0);
    end
  endfunction

  // Staged next-state logic. Each action of the model (apply config, start a
  // command, next transmit bit, begin a slot, set pin levels) happens at most
  // once per cycle, so each appears once here, enabled by flags that follow
  // the model's control flow (tools/tempo/lane.py, Lane.clock).
  reg        align_edge;
  reg        fin, sampled_end, want_cfg, want_try, start_cmd, start_auto, tx_slot, bslot, lv;
  reg [1:0]  bs_kind;
  reg [15:0] bs_len;
  reg [2:0]  h_kind;
  reg [15:0] h_data;
  reg [4:0]  h_n;
  reg [2:0]  h_flags;
  reg [3:0]  h_reg;
  reg        h_v, h_is_cfg, h_is_data;
  // a continuing receive (possibly behind one lw/start) is queued
  wire       cont_q = (q0_v && q0_kind == K_RX && q0_flags[0]) ||
                      (q0_v && (q0_kind == K_LW || q0_kind == K_START) && q1_v && q1_kind == K_RX && q1_flags[0]);

  always @* begin
    cfg_n = cfg; pins_n = pins; len0_n = len0; len1_n = len1; sym0_n = sym0; sym1_n = sym1;
    frac_n = frac; cr_n = cr; cf_n = cf; samp_n = samp; pat_n = pat; pate_n = pate;
    crc_n = crc; poly_n = poly; cap_n = cap; mode_n = mode;
    q0_v_n = q0_v; q0_kind_n = q0_kind; q0_data_n = q0_data; q0_n_n = q0_n; q0_flags_n = q0_flags; q0_reg_n = q0_reg;
    q1_v_n = q1_v; q1_kind_n = q1_kind; q1_data_n = q1_data; q1_n_n = q1_n; q1_flags_n = q1_flags; q1_reg_n = q1_reg;
    f0_v_n = f0_v; f0_word_n = f0_word; f0_count_n = f0_count; f0_end_n = f0_end; f0_err_n = f0_err;
    f1_v_n = f1_v; f1_word_n = f1_word; f1_count_n = f1_count; f1_end_n = f1_end; f1_err_n = f1_err;
    stat_n = stat; ev_start_n = ev_start; ev_stop_n = ev_stop; last_count_n = last_count;
    pend_v_n = pend_v; pend_t_n = pend_t;
    cur_v_n = cur_v; cur_kind_n = cur_kind; cur_n_n = cur_n; cur_flags_n = cur_flags; cur_data_n = cur_data;
    t_n = t; slot_len_n = slot_len; pre_n = pre; acc_n = acc; frozen_n = frozen; frozen_done_n = frozen_done;
    slot_kind_n = slot_kind; slot_v_n = slot_v; slot_sample_n = slot_sample; bits_left_n = bits_left;
    shift_n = shift; tx_last_n = tx_last; arb_lost_cmd_n = arb_lost_cmd; rx_complete_n = rx_complete;
    line_n = line; last_bit_n = last_bit; stuff_pending_n = stuff_pending; run_n = run;
    rline_n = rline; rlast_n = rlast; destuff_n = destuff; rerr_n = rerr; rrun_n = rrun;
    rword_n = rword; rcount_n = rcount; prev_level_n = prev_level;
    d_n = d; a_n = a; c_n = c; owned_n = owned; filt_n = filt; fcnt_n = fcnt; prev4_n = prev4;
    set_start = 1'b0; set_stop = 1'b0; done = 1'b0; ended = 1'b0; rx_push = 1'b0;
    rxp_word = 16'd0; rxp_count = 5'd0; rxp_end = 1'b0; rxp_err = 1'b0;
    sym = 16'd0; diff = 16'd0; crc_tmp = 16'd0; w = 16'd0; k = 5'd0; off = 8'd0;
    acc_sum = 9'd0; has_data = 1'b0; rrun_skip = 1'b0; x = 1'b0; lvl = 1'b0; cl = 1'b0; clk_rel = 1'b0;
    stuff = 3'd0; raw = 1'b0;
    fin = 1'b0; align_edge = 1'b0; sampled_end = 1'b0; want_cfg = 1'b0; want_try = 1'b0; start_cmd = 1'b0; start_auto = 1'b0;
    tx_slot = 1'b0; bslot = 1'b0; lv = 1'b0; bs_kind = SK_TX; bs_len = 16'd0;
    h_kind = 3'd0; h_data = 16'd0; h_n = 5'd0; h_flags = 3'd0; h_reg = 4'd0; h_v = 1'b0; h_is_cfg = 1'b0; h_is_data = 1'b0;
    off = 8'd3 + {5'd0, mode[11:9]};

    // ---- stage 0: inputs, filter, patterns
    flt = mode[11:9];
    for (r = 0; r < 4; r = r + 1) begin
      case (r)
        0: raw = in_pin(pins_in, pin_d(pins));
        1: raw = in_pin(pins_in, pin_i(pins));
        2: raw = in_pin(pins_in, pin_c(pins));
        default: raw = in_pin(pins_in, pin_a(pins));
      endcase
      cur4[r] = (flt == 3'd0) ? raw : filt[r];
      if (flt != 3'd0) begin
        if (raw == filt[r]) fcnt_n[r*3 +: 3] = 3'd0;
        else begin
          fcnt_n[r*3 +: 3] = fcnt[r*3 +: 3] + 3'd1;
          if (fcnt_n[r*3 +: 3] >= flt) begin
            filt_n[r] = raw;
            fcnt_n[r*3 +: 3] = 3'd0;
          end
        end
      end
    end
    in_d = cur4[0]; in_i = cur4[1]; in_c = cur4[2]; in_a = cur4[3];
    pat8 = {prev4, cur4};
    prev_i = prev4[1];
    prev4_n = cur4;
    m_start = (pat[15:8] != 8'd0) && ((pat8 & pat[15:8]) == (pat[7:0] & pat[15:8]));
    m_stop = (pate[15:8] != 8'd0) && ((pat8 & pate[15:8]) == (pate[7:0] & pate[15:8]));
    if (m_start) begin
      ev_start_n = 1'b1; set_start = 1'b1; stat_n[0] = 1'b1; cap_n = now;
    end
    if (m_stop) begin
      ev_stop_n = 1'b1; set_stop = 1'b1; stat_n[1] = 1'b1;
    end

    if (mode[7]) begin
      // ---- capture mode
      if (in_i != prev_i) begin
        rx_push = 1'b1; rxp_word = now; rxp_count = 5'd16; rxp_err = in_i;
      end
    end else if (cur_v) begin
      // ---- stage 1: running command (Lane.run_cycle)
      if (frozen) begin
        if (in_c) frozen_n = 1'b0;
        else done = 1'b1;
      end
      if (!done && pre == 8'd0) begin
        if (cfg[5] && cfg[2] && !frozen_done) begin
          if (slot_kind == SK_EMIT) clk_rel = (t == 17'd0) && !c[1];
          else if (cfg[3]) clk_rel = (t == {4'd0, cr}) && !c[1];
          if (clk_rel && !in_c) begin
            frozen_n = 1'b1;
            frozen_done_n = 1'b1;
            done = 1'b1;
          end
        end
        if (!done && slot_sample && t == {4'd0, samp}) begin
          // Lane.take_sample
          if (slot_kind == SK_TX && mode[8] && !d[1] && !in_i && !tx_last) begin
            if (!arb_lost_cmd) stat_n[6] = 1'b1;
            arb_lost_cmd_n = 1'b1;
          end
          if (FULL && cfg[6]) begin
            x = (in_i == rline);
            rline_n = in_i;
          end else begin
            x = in_i;
          end
          stuff = FULL ? cfg[10:8] : 3'd0;
          if (stuff != 3'd0) begin
            rrun_skip = destuff;
            if (destuff) begin
              destuff_n = 1'b0;
              if (x == rlast && (cfg[11] || x)) begin
                rerr_n = 1'b1;
                stat_n[4] = 1'b1;
              end
              rrun_n = cfg[11] ? 3'd1 : {2'd0, x};
            end else begin
              if (cfg[11]) rrun_n = (x == rlast) ? rrun + 3'd1 : 3'd1;
              else rrun_n = x ? rrun + 3'd1 : 3'd0;
              if (rrun_n >= stuff) destuff_n = 1'b1;
            end
            rlast_n = x;
          end
          if (!rrun_skip) begin
            if (cur_kind == K_RX && FULL) crc_n = crc_next(crc, poly, cfg[0], x);
            if (cfg[0]) rword_n = {rword[14:0], x};
            else rword_n = rword | ({15'd0, x} << rcount);
            rcount_n = rcount + 5'd1;
          end
          begin
            if (cur_kind == K_RX && rcount_n >= cur_n && !destuff_n) begin
              rx_push = 1'b1; rxp_word = rword_n; rxp_count = rcount_n; rxp_err = rerr_n;
              rword_n = 16'd0; rcount_n = 5'd0; rerr_n = 1'b0;
              if (cont_q) begin
                rx_complete_n = 1'b1;  // stay on the bit grid until the slot ends
              end else begin
                fin = 1'b1;
                done = 1'b1;
              end
            end
          end
        end
      end
      if (!done && m_stop && cur_kind == K_RX) begin
        rx_push = 1'b1; rxp_word = rword_n; rxp_count = rcount_n; rxp_end = 1'b1; rxp_err = rerr_n;
        rword_n = 16'd0; rcount_n = 5'd0; rerr_n = 1'b0;
        fin = 1'b1;
        done = 1'b1;
      end
      if (!done && FULL && mode[6:5] != 2'd0 && slot_sample && cur_kind == K_RX && in_i != prev_i &&
          (mode[6:5] == 2'd3 || (mode[6:5] == 2'd1 && in_i) || (mode[6:5] == 2'd2 && !in_i))) begin
        if (rx_complete) begin
          fin = 1'b1;          // the edge starts the next bit: hand over to the continuation
          align_edge = 1'b1;
        end else begin
          t_n = {9'd0, off >> mode[2:0]};
          pre_n = off & ((8'd1 << mode[2:0]) - 8'd1);
          lv = 1'b1;
        end
        done = 1'b1;
      end
      if (!done) begin
        pre_n = pre + 8'd1;
        if ((pre_n >> mode[2:0]) != 8'd0) begin
          pre_n = 8'd0;
          t_n = t + 17'd1;
        end
        if (t_n >= slot_len) begin
          // Lane.end_slot
          prev_level_n = d[0];
          if (cur_kind == K_TX && (bits_left != 5'd0 || stuff_pending)) begin
            tx_slot = 1'b1;
          end else begin
            if (cur_kind == K_TX && cur_flags[0]) begin
              rx_push = 1'b1; rxp_word = rword_n; rxp_count = rcount_n; rxp_err = rerr_n;
              rword_n = 16'd0; rcount_n = 5'd0; rerr_n = 1'b0;
            end
            if (cur_kind == K_RX && !rx_complete) begin
              bslot = 1'b1; bs_kind = SK_RX; bs_len = len0;
            end else begin
              fin = 1'b1;
            end
          end
        end else begin
          lv = 1'b1;
        end
      end
    end else if (q0_v) begin
      // ---- idle with work queued
      if (q0_kind == K_LW || q0_kind == K_START) want_cfg = 1'b1;
      else want_try = 1'b1;
    end

    // ---- stage 2: command end (Lane.finish_cmd, first half)
    if (fin) begin
      cur_v_n = 1'b0;
      frozen_n = 1'b0;
      has_data = (q0_v && (q0_kind == K_TX || q0_kind == K_RX || q0_kind == K_EMIT)) ||
                 (q1_v && (q1_kind == K_TX || q1_kind == K_RX || q1_kind == K_EMIT));
      if (cur_kind == K_EMIT) begin
        run_n = 3'd0;
        stuff_pending_n = 1'b0;
      end else if (mode[3] && !has_data) begin
        stat_n[3] = 1'b1;
      end
      if (q0_v && (q0_kind == K_LW || q0_kind == K_START)) want_cfg = 1'b1;
      want_try = 1'b1;
    end

    // ---- stage 3: apply one queued lw/start (Lane.apply_cfg)
    if (want_cfg) begin
      if (q0_kind == K_LW) begin
        case (q0_reg)
          4'd0:  cfg_n  = FULL ? q0_data[13:0] : (q0_data[13:0] & ~14'h0F40);
          4'd1:  begin pins_n = q0_data; owned_n = 3'b000; end
          4'd2:  len0_n = q0_data;
          4'd3:  len1_n = q0_data;
          4'd4:  sym0_n = FULL ? q0_data : (q0_data & 16'hDFFF);
          4'd5:  sym1_n = FULL ? q0_data : (q0_data & 16'hDFFF);
          4'd6:  frac_n = q0_data[7:0];
          4'd7:  cr_n = q0_data[12:0];
          4'd8:  cf_n = q0_data[12:0];
          4'd9:  samp_n = q0_data[12:0];
          4'd10: pat_n = q0_data;
          4'd11: pate_n = q0_data;
          4'd12: crc_n = FULL ? q0_data : 16'd0;
          4'd13: poly_n = FULL ? q0_data : 16'd0;
          4'd14: ;
          default: mode_n = FULL ? q0_data[11:0] : (q0_data[11:0] & 12'hF8F);
        endcase
      end else begin
        pend_v_n = 1'b1;
        pend_t_n = q0_data;
      end
    end
    // queue head after the optional pop
    h_v     = want_cfg ? q1_v : q0_v;
    h_kind  = want_cfg ? q1_kind : q0_kind;
    h_data  = want_cfg ? q1_data : q0_data;
    h_n     = want_cfg ? q1_n : q0_n;
    h_flags = want_cfg ? q1_flags : q0_flags;
    h_reg   = want_cfg ? q1_reg : q0_reg;
    h_is_data = h_v && (h_kind == K_TX || h_kind == K_RX || h_kind == K_EMIT);
    // an idle lane applying config does not also start a command this cycle
    if (want_cfg && !fin) want_try = 1'b0;

    // ---- stage 4: start condition (Lane.try_start)
    if (want_try && h_is_data) begin
      diff = (now + 16'd1) - pend_t_n;
      if (pend_v_n) begin
        if (!diff[15]) begin
          if (diff != 16'd0) stat_n[2] = 1'b1;
          pend_v_n = 1'b0;
          start_cmd = 1'b1;
        end
      end else if (h_kind == K_RX && cfg_n[12] && !h_flags[0]) begin
        if (m_start) begin
          start_cmd = 1'b1;
          start_auto = 1'b1;
        end
      end else begin
        start_cmd = 1'b1;
      end
    end

    // ---- pop the queue for the applied config and/or the started command
    if (want_cfg && start_cmd) begin
      q0_v_n = 1'b0; q1_v_n = 1'b0;
    end else if (want_cfg || start_cmd) begin
      q0_v_n = q1_v; q0_kind_n = q1_kind; q0_data_n = q1_data; q0_n_n = q1_n; q0_flags_n = q1_flags; q0_reg_n = q1_reg;
      q1_v_n = 1'b0;
    end

    // ---- stage 5: begin a command (Lane.begin_cmd)
    if (start_cmd) begin
      cur_v_n = 1'b1;
      cur_kind_n = h_kind;
      cur_data_n = h_data;
      cur_n_n = h_n;
      cur_flags_n = h_flags;
      arb_lost_cmd_n = 1'b0;
      rx_complete_n = 1'b0;
      frozen_done_n = 1'b0;
      if (h_kind == K_EMIT) begin
        bslot = 1'b1; bs_kind = SK_EMIT; bs_len = len0_n;
      end else if (h_kind == K_TX) begin
        crc_tmp = (FULL && mode_n[4]) ? ~crc_n : crc_n;
        if (h_flags[2]) shift_n = cfg_n[0] ? (crc_tmp >> (5'd16 - h_n)) : crc_tmp;
        else shift_n = h_data;
        bits_left_n = h_n;
        rword_n = 16'd0; rcount_n = 5'd0; rerr_n = 1'b0;
        rline_n = cur4[1];
        tx_slot = 1'b1;
      end else begin
        rword_n = 16'd0; rcount_n = 5'd0; rerr_n = 1'b0;
        // a continuing receive keeps the decoder state
        if (!h_flags[0]) begin
          rline_n = start_auto ? prev_i : cur4[1];
          rrun_n = 3'd0;
          destuff_n = 1'b0;
        end
        bslot = 1'b1; bs_kind = SK_RX; bs_len = len0_n;
      end
    end

    // ---- stage 6: next transmit bit (Lane.begin_tx_slot)
    if (tx_slot) begin
      stuff = FULL ? cfg_n[10:8] : 3'd0;
      if (stuff_pending_n) begin
        x = ~last_bit_n;
        stuff_pending_n = 1'b0;
        run_n = cfg_n[11] ? 3'd1 : {2'd0, x};
      end else begin
        if (cfg_n[0]) k = bits_left_n - 5'd1;
        else k = cur_n_n - bits_left_n;
        x = shift_n[k[3:0]];
        bits_left_n = bits_left_n - 5'd1;
        if (FULL && cur_flags_n[1] && !cur_flags_n[2]) crc_n = crc_next(crc_n, poly_n, cfg_n[0], x);
        if (stuff != 3'd0) begin
          if (cfg_n[11]) run_n = (x == last_bit_n) ? run_n + 3'd1 : 3'd1;
          else run_n = x ? run_n + 3'd1 : 3'd0;
          if (run_n >= stuff) stuff_pending_n = 1'b1;
        end
      end
      last_bit_n = x;
      tx_last_n = (bits_left_n == 5'd0) && !stuff_pending_n;
      if (FULL && cfg_n[6]) begin
        if (!x) line_n = ~line_n;
        slot_v_n = line_n;
      end else begin
        slot_v_n = x;
      end
      bslot = 1'b1; bs_kind = SK_TX; bs_len = slot_v_n ? len1_n : len0_n;
    end

    // ---- stage 7: begin a slot (Lane.begin_slot)
    if (bslot) begin
      slot_kind_n = bs_kind;
      slot_sample_n = (bs_kind == SK_RX) || (bs_kind == SK_TX && cur_flags_n[0]);
      acc_sum = {1'b0, acc_n} + {1'b0, frac_n};
      acc_n = acc_sum[7:0];
      slot_len_n = ((bs_len == 16'd0) ? 17'd1 : {1'b0, bs_len}) + {16'd0, acc_sum[8]};
      t_n = 17'd0;
      pre_n = 8'd0;
      frozen_done_n = 1'b0;
      if (bs_kind == SK_EMIT) begin
        w = cur_data_n;
        if (cfg_n[1] && w[0]) d_n = 2'b00; else d_n = {w[4], cfg_n[1] ? 1'b0 : w[0]};
        if (w[4] || cfg_n[1]) owned_n[0] = 1'b1;
        a_n = {w[5], w[1]};
        if (w[5]) owned_n[1] = 1'b1;
        if (cfg_n[2] && w[2]) c_n = 2'b00; else c_n = {w[6], cfg_n[2] ? 1'b0 : w[2]};
        if (w[6] || cfg_n[2]) owned_n[2] = 1'b1;
        line_n = w[0];
      end
      if (start_auto || align_edge) begin
        t_n = {9'd0, off >> mode_n[2:0]};
        pre_n = off & ((8'd1 << mode_n[2:0]) - 8'd1);
      end
      lv = 1'b1;
    end

    // ---- stage 8: pin levels for the next cycle (Lane.set_levels)
    if (lv) begin
      if (slot_kind_n == SK_TX) begin
        sym = slot_v_n ? sym1_n : sym0_n;
        lvl = (t_n < {4'd0, sym[12:0]}) ? sym[14] : sym[15];
        if (sym[13] && FULL) lvl = lvl ^ prev_level_n;
        if (arb_lost_cmd_n) lvl = 1'b1;
        if (cfg_n[1]) d_n = {~lvl, 1'b0};
        else d_n = {1'b1, lvl};
        owned_n[0] = 1'b1;
        if (cfg_n[7]) begin
          a_n = {1'b1, ~lvl};
          owned_n[1] = 1'b1;
        end
      end else if (slot_kind_n == SK_RX && cfg_n[13]) begin
        d_n[1] = 1'b0;
      end
      if ((slot_kind_n == SK_TX || slot_kind_n == SK_RX) && cfg_n[3]) begin
        cl = cfg_n[4] ^ ((t_n >= {4'd0, cr_n}) && (t_n < {4'd0, cf_n}));
        if (cfg_n[2]) c_n = {~cl, 1'b0};
        else c_n = {1'b1, cl};
        owned_n[2] = 1'b1;
      end
    end

    // ---- stage 9: idle levels after a command ends (Lane.hold_levels)
    if (fin && !cur_v_n) begin
      if (cfg_n[13]) d_n[1] = 1'b0;
      if (cfg_n[3] && owned_n[2]) begin
        cl = cfg_n[4];
        if (cfg_n[2]) c_n = {~cl, 1'b0};
        else c_n = {1'b1, cl};
      end
    end

    // ---- commit thread operations (Lane.commit)
    if (op_push) begin
      if (!q0_v_n) begin
        q0_v_n = 1'b1; q0_kind_n = op_kind; q0_data_n = op_data; q0_n_n = op_n; q0_flags_n = op_flags; q0_reg_n = op_reg;
      end else begin
        q1_v_n = 1'b1; q1_kind_n = op_kind; q1_data_n = op_data; q1_n_n = op_n; q1_flags_n = op_flags; q1_reg_n = op_reg;
      end
    end
    if (op_pop) begin
      last_count_n = f0_count;
      f0_v_n = f1_v; f0_word_n = f1_word; f0_count_n = f1_count; f0_end_n = f1_end; f0_err_n = f1_err;
      f1_v_n = 1'b0;
    end
    if (op_consume_start && !set_start) ev_start_n = 1'b0;
    if (op_consume_stop && !set_stop) ev_stop_n = 1'b0;
    if (op_clear != 16'd0) begin
      stat_n = stat_n & ~(op_clear[9:3] & ~{5'd0, set_stop, set_start});
      if (op_clear[3] && !set_start) ev_start_n = 1'b0;
      if (op_clear[4] && !set_stop) ev_stop_n = 1'b0;
    end
    if (op_stop) begin
      cur_v_n = 1'b0; q0_v_n = 1'b0; q1_v_n = 1'b0; frozen_n = 1'b0; pend_v_n = 1'b0;
      f0_v_n = 1'b0; f1_v_n = 1'b0; rx_push = 1'b0;
    end
    if (rx_push) begin
      if (f1_v_n) stat_n[5] = 1'b1;
      else if (f0_v_n) begin
        f1_v_n = 1'b1; f1_word_n = rxp_word; f1_count_n = rxp_count; f1_end_n = rxp_end; f1_err_n = rxp_err;
      end else begin
        f0_v_n = 1'b1; f0_word_n = rxp_word; f0_count_n = rxp_count; f0_end_n = rxp_end; f0_err_n = rxp_err;
      end
    end
  end
  always @(posedge clk) begin
    if (!rst_n) begin
      cfg <= 0; pins <= 16'hFFFF; len0 <= 16'd2; len1 <= 16'd2; sym0 <= 0; sym1 <= 0;
      frac <= 0; cr <= 0; cf <= 0; samp <= 0; pat <= 0; pate <= 0; crc <= 0; poly <= 0; cap <= 0; mode <= 0;
      q0_v <= 0; q0_kind <= 0; q0_data <= 0; q0_n <= 0; q0_flags <= 0; q0_reg <= 0;
      q1_v <= 0; q1_kind <= 0; q1_data <= 0; q1_n <= 0; q1_flags <= 0; q1_reg <= 0;
      f0_v <= 0; f0_word <= 0; f0_count <= 0; f0_end <= 0; f0_err <= 0;
      f1_v <= 0; f1_word <= 0; f1_count <= 0; f1_end <= 0; f1_err <= 0;
      stat <= 0; ev_start <= 0; ev_stop <= 0; last_count <= 0; pend_v <= 0; pend_t <= 0;
      cur_v <= 0; cur_kind <= 0; cur_n <= 0; cur_flags <= 0; cur_data <= 0;
      t <= 0; slot_len <= 0; pre <= 0; acc <= 0; frozen <= 0; frozen_done <= 0;
      slot_kind <= 0; slot_v <= 0; slot_sample <= 0; bits_left <= 0; shift <= 0; tx_last <= 0; arb_lost_cmd <= 0;
      rx_complete <= 0;
      line <= 1; last_bit <= 0; stuff_pending <= 0; run <= 0;
      rline <= 1; rlast <= 0; destuff <= 0; rerr <= 0; rrun <= 0; rword <= 0; rcount <= 0; prev_level <= 0;
      d <= 0; a <= 0; c <= 0; owned <= 0; filt <= 0; fcnt <= 0; prev4 <= 0;
    end else begin
      cfg <= cfg_n; pins <= pins_n; len0 <= len0_n; len1 <= len1_n; sym0 <= sym0_n; sym1 <= sym1_n;
      frac <= frac_n; cr <= cr_n; cf <= cf_n; samp <= samp_n; pat <= pat_n; pate <= pate_n;
      crc <= crc_n; poly <= poly_n; cap <= cap_n; mode <= mode_n;
      q0_v <= q0_v_n; q0_kind <= q0_kind_n; q0_data <= q0_data_n; q0_n <= q0_n_n; q0_flags <= q0_flags_n; q0_reg <= q0_reg_n;
      q1_v <= q1_v_n; q1_kind <= q1_kind_n; q1_data <= q1_data_n; q1_n <= q1_n_n; q1_flags <= q1_flags_n; q1_reg <= q1_reg_n;
      f0_v <= f0_v_n; f0_word <= f0_word_n; f0_count <= f0_count_n; f0_end <= f0_end_n; f0_err <= f0_err_n;
      f1_v <= f1_v_n; f1_word <= f1_word_n; f1_count <= f1_count_n; f1_end <= f1_end_n; f1_err <= f1_err_n;
      stat <= stat_n; ev_start <= ev_start_n; ev_stop <= ev_stop_n; last_count <= last_count_n;
      pend_v <= pend_v_n; pend_t <= pend_t_n;
      cur_v <= cur_v_n; cur_kind <= cur_kind_n; cur_n <= cur_n_n; cur_flags <= cur_flags_n; cur_data <= cur_data_n;
      t <= t_n; slot_len <= slot_len_n; pre <= pre_n; acc <= acc_n; frozen <= frozen_n; frozen_done <= frozen_done_n;
      slot_kind <= slot_kind_n; slot_v <= slot_v_n; slot_sample <= slot_sample_n; bits_left <= bits_left_n;
      shift <= shift_n; tx_last <= tx_last_n; arb_lost_cmd <= arb_lost_cmd_n; rx_complete <= rx_complete_n;
      line <= line_n; last_bit <= last_bit_n; stuff_pending <= stuff_pending_n; run <= run_n;
      rline <= rline_n; rlast <= rlast_n; destuff <= destuff_n; rerr <= rerr_n; rrun <= rrun_n;
      rword <= rword_n; rcount <= rcount_n; prev_level <= prev_level_n;
      d <= d_n; a <= a_n; c <= c_n; owned <= owned_n; filt <= filt_n; fcnt <= fcnt_n; prev4 <= prev4_n;
    end
  end

  // ----------------------------------------------------------- outputs
  always @* begin
    case (rd_idx)
      4'd0:  rd_data = {2'd0, cfg};
      4'd1:  rd_data = pins;
      4'd2:  rd_data = len0;
      4'd3:  rd_data = len1;
      4'd4:  rd_data = sym0;
      4'd5:  rd_data = sym1;
      4'd6:  rd_data = {8'd0, frac};
      4'd7:  rd_data = {3'd0, cr};
      4'd8:  rd_data = {3'd0, cf};
      4'd9:  rd_data = {3'd0, samp};
      4'd10: rd_data = pat;
      4'd11: rd_data = pate;
      4'd12: rd_data = crc;
      4'd13: rd_data = poly;
      4'd14: rd_data = cap;
      default: rd_data = {4'd0, mode};
    endcase
  end

  assign status = {last_count, 1'b0, stat[6:2], stat[1] | ev_stop, stat[0] | ev_start, f0_v, q1_v, cur_v | q0_v};
  assign q_full = q1_v;
  assign idle = !cur_v && !q0_v;
  assign rx_avail = f0_v;
  assign rx_word = f0_word;
  assign rx_end = f0_end;
  assign rx_err = f0_err;
  assign ev_start_o = ev_start;
  assign ev_stop_o = ev_stop;
  assign cap_o = cap;
  assign pins_cfg = pins;
  assign d_o = d;
  assign a_o = a;
  assign c_o = c;
  assign owned_o = owned;
  assign running_o = cur_v;

  wire _unused = &{in_d, in_a, h_reg, 1'b0};
endmodule
