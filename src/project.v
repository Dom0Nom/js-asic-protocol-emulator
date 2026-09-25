/*
 * Tempo: a timed line-code processor for bit-banged protocols.
 * Top level: input synchroniser, instruction SRAM, core, registered pads.
 * Architecture: docs/architecture.md.
 *
 * Copyright (c) 2026 Dom0Nom
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_dom0nom_protoemu (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);
  // Two-flop synchroniser on every input. Index order: uio_in[7:0], ui_in[7:0].
  reg [15:0] sync1, sync2;
  always @(posedge clk) begin
    if (!rst_n) begin
      sync1 <= 16'd0;
      sync2 <= 16'd0;
    end else begin
      sync1 <= {ui_in, uio_in};
      sync2 <= sync1;
    end
  end

  wire [9:0]  imem_addr;
  wire        imem_ren, imem_wen;
  wire [15:0] imem_din, imem_dout;
  wire [15:0] out_val, out_oe;

  tempo_core core (
      .clk(clk), .rst_n(rst_n), .pins_in(sync2),
      .imem_addr(imem_addr), .imem_ren(imem_ren), .imem_wen(imem_wen),
      .imem_din(imem_din), .imem_dout(imem_dout),
      .out_val(out_val), .out_oe(out_oe),
      .now_o(), .commit_o(), .commit_tid(), .commit_pc(), .commit_wr(), .commit_rd(), .commit_val()
  );

  RM_IHPSG13_1P_1024x16_c2_bm_bist imem (
      .A_CLK(clk), .A_MEN(imem_ren | imem_wen), .A_WEN(imem_wen), .A_REN(imem_ren),
      .A_ADDR(imem_addr), .A_DIN(imem_din), .A_DLY(1'b1), .A_DOUT(imem_dout), .A_BM(16'hFFFF),
      .A_BIST_CLK(1'b0), .A_BIST_EN(1'b0), .A_BIST_MEN(1'b0), .A_BIST_WEN(1'b0), .A_BIST_REN(1'b0),
      .A_BIST_ADDR(10'd0), .A_BIST_DIN(16'd0), .A_BIST_BM(16'd0)
  );

  assign uio_out = out_val[7:0];
  assign uio_oe  = out_oe[7:0];
  // Output index 15 is the host MISO. It goes to uo[3], the RP2350's SPI0 RX
  // pin on the demo board, so output index 11 takes uo[7].
  assign uo_out  = {out_val[11], out_val[14:12], out_val[15], out_val[10:8]};

  wire _unused = &{ena, out_oe[15:8], 1'b0};
endmodule
