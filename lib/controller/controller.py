#!/usr/bin/env python

"""
Copyright (c) 2006-2012 sqlmap developers (http://sqlmap.org/)
See the file 'doc/COPYING' for copying permission
"""

import os
import re

from lib.controller.action import action
from lib.controller.checks import checkSqlInjection
from lib.controller.checks import checkDynParam
from lib.controller.checks import checkStability
from lib.controller.checks import checkString
from lib.controller.checks import checkRegexp
from lib.controller.checks import checkConnection
from lib.controller.checks import checkNullConnection
from lib.controller.checks import checkWaf
from lib.controller.checks import heuristicCheckSqlInjection
from lib.core.agent import agent
from lib.core.common import extractRegexResult
from lib.core.common import getFilteredPageContent
from lib.core.common import getPublicTypeMembers
from lib.core.common import getUnicode
from lib.core.common import hashDBRetrieve
from lib.core.common import hashDBWrite
from lib.core.common import intersect
from lib.core.common import parseTargetUrl
from lib.core.common import randomStr
from lib.core.common import readInput
from lib.core.common import safeCSValue
from lib.core.common import showHttpErrorCodes
from lib.core.common import urlencode
from lib.core.common import urldecode
from lib.core.data import conf
from lib.core.data import kb
from lib.core.data import logger
from lib.core.enums import HASHDB_KEYS
from lib.core.enums import HEURISTIC_TEST
from lib.core.enums import HTTPMETHOD
from lib.core.enums import PAYLOAD
from lib.core.enums import PLACE
from lib.core.exception import exceptionsTuple
from lib.core.exception import SqlmapNoneDataException
from lib.core.exception import SqlmapNotVulnerableException
from lib.core.exception import SqlmapSilentQuitException
from lib.core.exception import SqlmapValueException
from lib.core.exception import SqlmapUserQuitException
from lib.core.settings import ASP_NET_CONTROL_REGEX
from lib.core.settings import DEFAULT_GET_POST_DELIMITER
from lib.core.settings import EMPTY_FORM_FIELDS_REGEX
from lib.core.settings import IGNORE_PARAMETERS
from lib.core.settings import LOW_TEXT_PERCENT
from lib.core.settings import HOST_ALIASES
from lib.core.settings import REFERER_ALIASES
from lib.core.settings import USER_AGENT_ALIASES
from lib.core.target import initTargetEnv
from lib.core.target import setupTargetEnv
from thirdparty.pagerank.pagerank import get_pagerank

def _selectInjection():
    """
    Selection function for injection place, parameters and type.
    """

    points = {}

    for injection in kb.injections:
        place = injection.place
        parameter = injection.parameter
        ptype = injection.ptype

        point = (place, parameter, ptype)

        if point not in points:
            points[point] = injection
        else:
            for key in points[point].keys():
                if key != 'data':
                    points[point][key] = points[point][key] or injection[key]
            points[point]['data'].update(injection['data'])

    if len(points) == 1:
        kb.injection = kb.injections[0]

    elif len(points) > 1:
        message = "there were multiple injection points, please select "
        message += "the one to use for following injections:\n"

        points = []

        for i in xrange(0, len(kb.injections)):
            place = kb.injections[i].place
            parameter = kb.injections[i].parameter
            ptype = kb.injections[i].ptype
            point = (place, parameter, ptype)

            if point not in points:
                points.append(point)
                ptype = PAYLOAD.PARAMETER[ptype] if isinstance(ptype, int) else ptype

                message += "[%d] place: %s, parameter: " % (i, place)
                message += "%s, type: %s" % (parameter, ptype)

                if i == 0:
                    message += " (default)"

                message += "\n"

        message += "[q] Quit"
        select = readInput(message, default="0")

        if select.isdigit() and int(select) < len(kb.injections) and int(select) >= 0:
            index = int(select)
        elif select[0] in ("Q", "q"):
            raise SqlmapUserQuitException
        else:
            errMsg = "invalid choice"
            raise SqlmapValueException(errMsg)

        kb.injection = kb.injections[index]

def _formatInjection(inj):
    data = "Place: %s\n" % inj.place
    data += "Parameter: %s\n" % inj.parameter

    for stype, sdata in inj.data.items():
        title = sdata.title
        vector = sdata.vector
        comment = sdata.comment
        payload = agent.adjustLateValues(sdata.payload)
        if inj.place == PLACE.CUSTOM_HEADER:
            payload = payload.split(',', 1)[1]
        if stype == PAYLOAD.TECHNIQUE.UNION:
            count = re.sub(r"(?i)(\(.+\))|(\blimit[^A-Za-z]+)", "", sdata.payload).count(',') + 1
            title = re.sub(r"\d+ to \d+", str(count), title)
            vector = agent.forgeUnionQuery("[QUERY]", vector[0], vector[1], vector[2], None, None, vector[5], vector[6])
            if count == 1:
                title = title.replace("columns", "column")
        elif comment:
            vector = "%s%s" % (vector, comment)
        data += "    Type: %s\n" % PAYLOAD.SQLINJECTION[stype]
        data += "    Title: %s\n" % title
        data += "    Payload: %s\n" % payload
        data += "    Vector: %s\n\n" % vector if conf.verbose > 1 else "\n"

    return data

def _showInjections():
    header = "sqlmap identified the following injection points with "
    header += "a total of %d HTTP(s) requests" % kb.testQueryCount

    data = "".join(set(map(lambda x: _formatInjection(x), kb.injections))).rstrip("\n")

    conf.dumper.string(header, data)

    if conf.tamper:
        warnMsg = "changes made by tampering scripts are not "
        warnMsg += "included in shown payload content(s)"
        logger.warn(warnMsg)

    if conf.hpp:
        warnMsg = "changes made by HTTP parameter pollution are not "
        warnMsg += "included in shown payload content(s)"
        logger.warn(warnMsg)

def _randomFillBlankFields(value):
    retVal = value

    if extractRegexResult(EMPTY_FORM_FIELDS_REGEX, value):
        message = "do you want to fill blank fields with random values? [Y/n] "
        test = readInput(message, default="Y")
        if not test or test[0] in ("y", "Y"):
            for match in re.finditer(EMPTY_FORM_FIELDS_REGEX, retVal):
                item = match.group("result")
                if not any(_ in item for _ in IGNORE_PARAMETERS) and not re.search(ASP_NET_CONTROL_REGEX, item):
                    if item[-1] == DEFAULT_GET_POST_DELIMITER:
                        retVal = retVal.replace(item, "%s%s%s" % (item[:-1], randomStr(), DEFAULT_GET_POST_DELIMITER))
                    else:
                        retVal = retVal.replace(item, "%s%s" % (item, randomStr()))

    return retVal

def _saveToHashDB():
    injections = hashDBRetrieve(HASHDB_KEYS.KB_INJECTIONS, True) or []
    injections.extend(_ for _ in kb.injections if _ and _.place is not None and _.parameter is not None)

    _ = dict()
    for injection in injections:
        key = (injection.place, injection.parameter, injection.ptype)
        if key not in _:
            _[key] = injection
        else:
            _[key].data.update(injection.data)
    hashDBWrite(HASHDB_KEYS.KB_INJECTIONS, _.values(), True)

    _ = hashDBRetrieve(HASHDB_KEYS.KB_ABS_FILE_PATHS, True) or set()
    _.update(kb.absFilePaths)
    hashDBWrite(HASHDB_KEYS.KB_ABS_FILE_PATHS, _, True)

    if not hashDBRetrieve(HASHDB_KEYS.KB_CHARS):
        hashDBWrite(HASHDB_KEYS.KB_CHARS, kb.chars, True)

    if not hashDBRetrieve(HASHDB_KEYS.KB_DYNAMIC_MARKINGS):
        hashDBWrite(HASHDB_KEYS.KB_DYNAMIC_MARKINGS, kb.dynamicMarkings, True)

def _saveToResultsFile():
    if not conf.resultsFP:
        return

    results = {}
    techniques = dict(map(lambda x: (x[1], x[0]), getPublicTypeMembers(PAYLOAD.TECHNIQUE)))

    for inj in kb.injections:
        if inj.place is None or inj.parameter is None:
            continue

        key = (inj.place, inj.parameter)
        if key not in results:
            results[key] = []

        results[key].extend(inj.data.keys())

    for key, value in results.items():
        place, parameter = key
        line = "%s,%s,%s,%s%s" % (safeCSValue(kb.originalUrls.get(conf.url) or conf.url), place, parameter, "".join(map(lambda x: techniques[x][0].upper(), sorted(value))), os.linesep)
        conf.resultsFP.writelines(line)

    if not results:
        line = "%s,,,%s" % (conf.url, os.linesep)
        conf.resultsFP.writelines(line)

def start():
    """
    This function calls a function that performs checks on both URL
    stability and all GET, POST, Cookie and User-Agent parameters to
    check if they are dynamic and SQL injection affected
    """

    if conf.direct:
        initTargetEnv()
        setupTargetEnv()
        action()
        return True

    if conf.url and not any((conf.forms, conf.crawlDepth)):
        kb.targets.add((conf.url, conf.method, conf.data, conf.cookie))

    if conf.configFile and not kb.targets:
        errMsg = "you did not edit the configuration file properly, set "
        errMsg += "the target url, list of targets or google dork"
        logger.error(errMsg)
        return False

    if kb.targets and len(kb.targets) > 1:
        infoMsg = "sqlmap got a total of %d targets" % len(kb.targets)
        logger.info(infoMsg)

    hostCount = 0

    for targetUrl, targetMethod, targetData, targetCookie in kb.targets:
        try:
            conf.url = targetUrl
            conf.method = targetMethod
            conf.data = targetData
            conf.cookie = targetCookie

            initTargetEnv()
            parseTargetUrl()

            testSqlInj = False

            if PLACE.GET in conf.parameters and not any([conf.data, conf.testParameter]):
                for parameter in re.findall(r"([^=]+)=([^%s]+%s?|\Z)" % (conf.pDel or ";", conf.pDel or ";"), conf.parameters[PLACE.GET]):
                    paramKey = (conf.hostname, conf.path, PLACE.GET, parameter[0])

                    if paramKey not in kb.testedParams:
                        testSqlInj = True
                        break
            else:
                paramKey = (conf.hostname, conf.path, None, None)
                if paramKey not in kb.testedParams:
                    testSqlInj = True

            testSqlInj &= (conf.hostname, conf.path, None, None) not in kb.testedParams
            testSqlInj &= conf.hostname not in kb.vulnHosts

            if not testSqlInj:
                infoMsg = "skipping '%s'" % targetUrl
                logger.info(infoMsg)
                continue

            if conf.multipleTargets:
                hostCount += 1

                if conf.forms:
                    message = "[#%d] form:\n%s %s" % (hostCount, conf.method or HTTPMETHOD.GET, targetUrl)
                else:
                    message = "url %d:\n%s %s%s" % (hostCount, conf.method or HTTPMETHOD.GET, targetUrl, " (PageRank: %s)" % get_pagerank(targetUrl) if conf.googleDork and conf.pageRank else "")

                if conf.cookie:
                    message += "\nCookie: %s" % conf.cookie

                if conf.data is not None:
                    message += "\nPOST data: %s" % urlencode(conf.data) if conf.data else ""

                if conf.forms:
                    if conf.method == HTTPMETHOD.GET and targetUrl.find("?") == -1:
                        continue

                    message += "\ndo you want to test this form? [Y/n/q] "
                    test = readInput(message, default="Y")

                    if not test or test[0] in ("y", "Y"):
                        if conf.method == HTTPMETHOD.POST:
                            message = "Edit POST data [default: %s]%s: " % (urlencode(conf.data) if conf.data else "None", " (Warning: blank fields detected)" if conf.data and extractRegexResult(EMPTY_FORM_FIELDS_REGEX, conf.data) else "")
                            conf.data = readInput(message, default=conf.data)
                            conf.data = _randomFillBlankFields(conf.data)
                            conf.data = urldecode(conf.data) if conf.data and urlencode(DEFAULT_GET_POST_DELIMITER, None) not in conf.data else conf.data

                        elif conf.method == HTTPMETHOD.GET:
                            if targetUrl.find("?") > -1:
                                firstPart = targetUrl[:targetUrl.find("?")]
                                secondPart = targetUrl[targetUrl.find("?") + 1:]
                                message = "Edit GET data [default: %s]: " % secondPart
                                test = readInput(message, default=secondPart)
                                test = _randomFillBlankFields(test)
                                conf.url = "%s?%s" % (firstPart, test)

                        parseTargetUrl()

                    elif test[0] in ("n", "N"):
                        continue
                    elif test[0] in ("q", "Q"):
                        break

                else:
                    message += "\ndo you want to test this url? [Y/n/q]"
                    test = readInput(message, default="Y")

                    if not test or test[0] in ("y", "Y"):
                        pass
                    elif test[0] in ("n", "N"):
                        continue
                    elif test[0] in ("q", "Q"):
                        break

                    infoMsg = "testing url '%s'" % targetUrl
                    logger.info(infoMsg)

            setupTargetEnv()

            if not checkConnection(suppressOutput=conf.forms) or not checkString() or not checkRegexp():
                continue

            if conf.checkWaf:
                checkWaf()

            if conf.nullConnection:
                checkNullConnection()

            if (len(kb.injections) == 0 or (len(kb.injections) == 1 and kb.injections[0].place is None)) \
                and (kb.injection.place is None or kb.injection.parameter is None):

                if not any((conf.string, conf.notString, conf.regexp)) and PAYLOAD.TECHNIQUE.BOOLEAN in conf.tech:
                    # NOTE: this is not needed anymore, leaving only to display
                    # a warning message to the user in case the page is not stable
                    checkStability()

                # Do a little prioritization reorder of a testable parameter list
                parameters = conf.parameters.keys()

                # Order of testing list (first to last)
                orderList = (PLACE.CUSTOM_POST, PLACE.CUSTOM_HEADER, PLACE.URI, PLACE.POST, PLACE.GET)

                for place in orderList[::-1]:
                    if place in parameters:
                        parameters.remove(place)
                        parameters.insert(0, place)

                proceed = True
                for place in parameters:
                    # Test User-Agent and Referer headers only if
                    # --level >= 3
                    skip = (place == PLACE.USER_AGENT and conf.level < 3)
                    skip |= (place == PLACE.REFERER and conf.level < 3)

                    # Test Host header only if
                    # --level >= 5
                    skip |= (place == PLACE.HOST and conf.level < 5)

                    # Test Cookie header only if --level >= 2
                    skip |= (place == PLACE.COOKIE and conf.level < 2)

                    skip |= (place == PLACE.USER_AGENT and intersect(USER_AGENT_ALIASES, conf.skip, True) not in ([], None))
                    skip |= (place == PLACE.REFERER and intersect(REFERER_ALIASES, conf.skip, True) not in ([], None))
                    skip |= (place == PLACE.COOKIE and intersect(PLACE.COOKIE, conf.skip, True) not in ([], None))

                    skip &= not (place == PLACE.USER_AGENT and intersect(USER_AGENT_ALIASES, conf.testParameter, True))
                    skip &= not (place == PLACE.REFERER and intersect(REFERER_ALIASES, conf.testParameter, True))
                    skip &= not (place == PLACE.HOST and intersect(HOST_ALIASES, conf.testParameter, True))

                    if skip:
                        continue

                    if place not in conf.paramDict:
                        continue

                    paramDict = conf.paramDict[place]

                    for parameter, value in paramDict.items():
                        if not proceed:
                            break

                        kb.vainRun = False
                        testSqlInj = True
                        paramKey = (conf.hostname, conf.path, place, parameter)

                        if paramKey in kb.testedParams:
                            testSqlInj = False

                            infoMsg = "skipping previously processed %s parameter '%s'" % (place, parameter)
                            logger.info(infoMsg)

                        elif parameter in conf.testParameter:
                            pass

                        elif parameter == conf.rParam:
                            testSqlInj = False

                            infoMsg = "skipping randomizing %s parameter '%s'" % (place, parameter)
                            logger.info(infoMsg)

                        elif parameter in conf.skip:
                            testSqlInj = False

                            infoMsg = "skipping %s parameter '%s'" % (place, parameter)
                            logger.info(infoMsg)

                        # Ignore session-like parameters for --level < 4
                        elif conf.level < 4 and parameter.upper() in IGNORE_PARAMETERS:
                            testSqlInj = False

                            infoMsg = "ignoring %s parameter '%s'" % (place, parameter)
                            logger.info(infoMsg)

                        elif PAYLOAD.TECHNIQUE.BOOLEAN in conf.tech:
                            check = checkDynParam(place, parameter, value)

                            if not check:
                                warnMsg = "%s parameter '%s' does not appear dynamic" % (place, parameter)
                                logger.warn(warnMsg)

                            else:
                                infoMsg = "%s parameter '%s' is dynamic" % (place, parameter)
                                logger.info(infoMsg)

                        kb.testedParams.add(paramKey)

                        if testSqlInj:
                            check = heuristicCheckSqlInjection(place, parameter)

                            if check != HEURISTIC_TEST.POSITIVE:
                                if conf.smart or (kb.ignoreCasted and check == HEURISTIC_TEST.CASTED):
                                    infoMsg = "skipping %s parameter '%s'" % (place, parameter)
                                    logger.info(infoMsg)
                                    continue

                            infoMsg = "testing for SQL injection on %s " % place
                            infoMsg += "parameter '%s'" % parameter
                            logger.info(infoMsg)

                            injection = checkSqlInjection(place, parameter, value)
                            proceed = not kb.endDetection

                            if injection is not None and injection.place is not None:
                                kb.injections.append(injection)

                                # In case when user wants to end detection phase (Ctrl+C)
                                if not proceed:
                                    break

                                msg = "%s parameter '%s' " % (injection.place, injection.parameter)
                                msg += "is vulnerable. Do you want to keep testing the others (if any)? [y/N] "
                                test = readInput(msg, default="N")

                                if test[0] not in ("y", "Y"):
                                    proceed = False
                                    paramKey = (conf.hostname, conf.path, None, None)
                                    kb.testedParams.add(paramKey)
                            else:
                                warnMsg = "%s parameter '%s' is not " % (place, parameter)
                                warnMsg += "injectable"
                                logger.warn(warnMsg)

            if len(kb.injections) == 0 or (len(kb.injections) == 1 and kb.injections[0].place is None):
                if kb.vainRun and not conf.multipleTargets:
                    errMsg = "no parameter(s) found for testing in the provided data "
                    errMsg += "(e.g. GET parameter 'id' in 'www.site.com/index.php?id=1')"
                    raise SqlmapNoneDataException(errMsg)
                else:
                    errMsg = "all tested parameters appear to be not injectable."

                    if conf.level < 5 or conf.risk < 3:
                        errMsg += " Try to increase '--level'/'--risk' values "
                        errMsg += "to perform more tests."

                    if isinstance(conf.tech, list) and len(conf.tech) < 5:
                        errMsg += " Rerun without providing the option '--technique'."

                    if not conf.textOnly and kb.originalPage:
                        percent = (100.0 * len(getFilteredPageContent(kb.originalPage)) / len(kb.originalPage))

                        if kb.dynamicMarkings:
                            errMsg += " You can give it a go with the switch '--text-only' "
                            errMsg += "if the target page has a low percentage "
                            errMsg += "of textual content (~%.2f%% of " % percent
                            errMsg += "page content is text)."
                        elif percent < LOW_TEXT_PERCENT and not kb.errorIsNone:
                            errMsg += " Please retry with the switch '--text-only' "
                            errMsg += "(along with --technique=BU) as this case "
                            errMsg += "looks like a perfect candidate "
                            errMsg += "(low textual content along with inability "
                            errMsg += "of comparison engine to detect at least "
                            errMsg += "one dynamic parameter)."

                    if kb.heuristicTest == HEURISTIC_TEST.POSITIVE:
                        errMsg += " As heuristic test turned out positive you are "
                        errMsg += "strongly advised to continue on with the tests. "
                        errMsg += "Please, consider usage of tampering scripts as "
                        errMsg += "your target might filter the queries."

                    if not conf.string and not conf.notString and not conf.regexp:
                        errMsg += " Also, you can try to rerun by providing "
                        errMsg += "either a valid value for option '--string' "
                        errMsg += "(or '--regexp')"
                    elif conf.string:
                        errMsg += " Also, you can try to rerun by providing a "
                        errMsg += "valid value for option '--string' as perhaps the string you "
                        errMsg += "have choosen does not match "
                        errMsg += "exclusively True responses"
                    elif conf.regexp:
                        errMsg += " Also, you can try to rerun by providing a "
                        errMsg += "valid value for option '--regexp' as perhaps the regular "
                        errMsg += "expression that you have choosen "
                        errMsg += "does not match exclusively True responses"

                    raise SqlmapNotVulnerableException(errMsg)
            else:
                # Flush the flag
                kb.testMode = False

                _saveToResultsFile()
                _saveToHashDB()
                _showInjections()
                _selectInjection()

            if kb.injection.place is not None and kb.injection.parameter is not None:
                if conf.multipleTargets:
                    message = "do you want to exploit this SQL injection? [Y/n] "
                    exploit = readInput(message, default="Y")

                    condition = not exploit or exploit[0] in ("y", "Y")
                else:
                    condition = True

                if condition:
                    action()

        except KeyboardInterrupt:
            if conf.multipleTargets:
                warnMsg = "user aborted in multiple target mode"
                logger.warn(warnMsg)

                message = "do you want to skip to the next target in list? [Y/n/q]"
                test = readInput(message, default="Y")

                if not test or test[0] in ("y", "Y"):
                    pass
                elif test[0] in ("n", "N"):
                    return False
                elif test[0] in ("q", "Q"):
                    raise SqlmapUserQuitException
            else:
                raise

        except SqlmapUserQuitException:
            raise

        except SqlmapSilentQuitException:
            raise

        except exceptionsTuple, e:
            e = getUnicode(e)

            if conf.multipleTargets:
                e += ", skipping to the next %s" % ("form" if conf.forms else "url")
                logger.error(e)
            else:
                logger.critical(e)
                return False

        finally:
            showHttpErrorCodes()

            if kb.maxConnectionsFlag:
                warnMsg = "it appears that the target "
                warnMsg += "has a maximum connections "
                warnMsg += "constraint"
                logger.warn(warnMsg)

    if kb.dataOutputFlag and not conf.multipleTargets:
        logger.info("fetched data logged to text files under '%s'" % conf.outputPath)

    if conf.multipleTargets and conf.resultsFilename:
        infoMsg = "you can find results of scanning in multiple targets "
        infoMsg += "mode inside the CSV file '%s'" % conf.resultsFilename
        logger.info(infoMsg)

    return True
