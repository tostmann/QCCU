# Fremdbestandteile

| Bestandteil | Fassung | Lizenz | Pflicht |
|---|---|---|---|
| LUFA | 2009 | permissiv (MIT-artig) | Vermerk unten |
| avr-libc | 2.2.1 | BSD-3-Clause | Vermerk unten |
| libgcc | avr-gcc 14.2 | GPL + GCC Runtime Library Exception | keine |

---

## LUFA

    LUFA Library
    Copyright (C) Dean Camera, 2009.
    dean [at] fourwalledcubicle [dot] com
    www.fourwalledcubicle.com

    Permission to use, copy, modify, and distribute this software and its
    documentation for any purpose and without fee is hereby granted, provided
    that the above copyright notice appear in all copies and that both that the
    copyright notice and this permission notice and warranty disclaimer appear
    in supporting documentation, and that the name of the author not be used in
    advertising or publicity pertaining to distribution of the software without
    specific, written prior permission.

    The author disclaim all warranties with regard to this software, including
    all implied warranties of merchantability and fitness. In no event shall the
    author be liable for any special, indirect or consequential damages or any
    damages whatsoever resulting from loss of use, data or profits, whether in
    an action of contract, negligence or other tortious action, arising out of
    or in connection with the use or performance of this software.

## avr-libc

    Copyright (c) 2002-2013 Joerg Wunsch
    Copyright (c) 2003-2005 Keith Gudger
    und weitere Beitragende — siehe die Lizenzdatei der avr-libc.

    Weitergabe in Quell- und Binärform, mit oder ohne Veränderung, ist
    gestattet, sofern der Copyright-Vermerk, diese Bedingungen und der
    Gewährleistungsausschluss erhalten bleiben; der Name der Beitragenden darf
    ohne vorherige schriftliche Erlaubnis nicht zur Bewerbung abgeleiteter
    Produkte verwendet werden (BSD-3-Clause).

## libgcc

Eingebunden sind Hilfsroutinen des Übersetzers (`__ashldi3`, `__bswapsi2` und
weitere). Sie stehen unter der GPL **mit der GCC Runtime Library Exception**,
die das Verteilen des Kompilats unter beliebiger Lizenz ausdrücklich gestattet.
Daraus folgt keine Pflicht für dieses Programm.
